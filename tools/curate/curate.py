"""One-time curation from an archived Qiskit benchmark tree, never from a candidate.

Run: python tools/curate/curate.py --source /path/to/archived/0131cbbcc
Only imports Qiskit from the pinned verifier environment. Upstream files lacking
redistribution provenance are replaced by declared upstream synthesis constructions
as permitted by implementation-plan section 3.8.
"""

import argparse
import copy
import importlib.util
import json
import random
import shutil
import tempfile
from pathlib import Path

import numpy as np
import qiskit
from qiskit import QuantumCircuit
from qiskit.quantum_info import random_unitary
from qiskit.transpiler import CouplingMap, Target

from qtb.canonical import digest, write_circuit, write_json
from qtb.evaluator.statistics import hierarchical_weights
from qtb_worker.adapter import export_circuit, export_target, import_circuit

ROOT = Path(__file__).resolve().parents[2]
ITERATIONS = ["qft_n100", "square_heisenberg_n100", "qaoa_ba_n100_3reps"]
RESTRICTED = {
    "hwb12",
    "revlib_4gt10_v1_81",
    "revlib_4mod5_v0_19",
    "revlib_mod8_10_178",
    "revlib_cnt3_5_179",
    "revlib_cnt3_5_180",
}
REPLACEMENTS = {
    "revlib_4gt10_v1_81": ("mcx_kg24_n5", "ft_mcx_5", "utils.mcx_circuit"),
    "revlib_mod8_10_178": ("mcx_kg24_n6", "ft_mcx_6", "utils.mcx_circuit"),
    "revlib_cnt3_5_179": (
        "adder_modular_v17_n8",
        "ft_modular_adder_8",
        "utils.modular_adder_circuit",
    ),
    "revlib_cnt3_5_180": (
        "adder_modular_v17_n16",
        "ft_modular_adder_16",
        "utils.modular_adder_circuit",
    ),
    "revlib_4mod5_v0_19": (
        "adder_modular_v17_n6",
        "ft_modular_adder_6",
        "utils.modular_adder_circuit",
    ),
    "hwb12": ("multiplier_h18_n20", "ft_multiplier_20", "utils.multiplier_circuit"),
}
OPTIONS = dict(
    approximation_degree=1.0,
    qubits_initially_zero=True,
    initial_layout=None,
    layout_method=None,
    routing_method=None,
    translation_method=None,
    scheduling_method=None,
)


def load_probe(source):
    spec = importlib.util.spec_from_file_location(
        "curation_probe", ROOT / "tools/probes/confirm_probe.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.configure_source(str(source))
    return module


def policy(profile):
    prefix = "CA" if profile == "confirm-profile" else "IA"
    required = [
        "harness/roundtrip",
        "harness/qualification",
        "calibration/quality",
        "calibration/cost",
        "baseline/preflight",
        "audit/determinism",
        f"{prefix}1/C0",
        f"{prefix}1/C1-C5",
        f"{prefix}1/C6",
        f"{prefix}1/C7",
        f"{prefix}1/upstream",
        f"{prefix}1/stage-coverage",
        f"{prefix}2/improvement",
        f"{prefix}5/timing",
        f"{prefix}6/completeness",
    ]
    if prefix == "CA":
        required += ["CA3/breadth", "CA1/C1-lite", "CA5/confirm-timing", "CA5/memory"]
    return dict(
        format="qtb-policy/1",
        profile=profile,
        version=1,
        required_ids=required,
        quality_prerequisites=["harness/roundtrip", "audit/determinism"],
        practical_ratio=0.99 if prefix == "CA" else 1.0,
        quality_cap=1.05,
        improvement_multiplier=2.0,
        guard_multiplier=3.0,
        rng_seed=20260924,
        bootstrap_replicates=10000,
        iterations_groups=ITERATIONS,
        measurement_protocol=dict(
            seed_blocks={"B0": [0, 100], "KB1": [100, 200], "KB2": [200, 300]},
            quality_batch_size=25,
            timing_rounds=10,
            warmups=1,
            minimum_calls=3,
            minimum_ns=1000000000,
            companion_seeds=list(range(20)),
            companion_rounds=3,
            memory_processes=5,
            calibration_rounds=30,
            calibration_memory_processes=10,
            rerun_multiplier=2,
            calibration_expiry_days=30,
            audit_fraction=0.05,
            audit_minimum=10,
            output_retention_bytes=8 * 1024 * 1024,
        ),
        tolerances=dict(operator_rtol=1e-7, operator_atol=1e-8, state_atol=1e-8),
        qualification=dict(
            status="unqualified",
            reason="Requires known-outcome validation and inspected controlled-runner evidence",
        ),
    )


def curate(source, root=ROOT):
    if qiskit.__version__ != "2.5.2":
        raise RuntimeError("Curate only in the pinned Qiskit 2.5.2 verifier environment")
    probe = load_probe(source)
    catalog = json.loads((ROOT / "tools/curate/catalog.json").read_text())
    for row in catalog:
        old = row["input_group"]
        if old in REPLACEMENTS:
            name, recipe, source_name = REPLACEMENTS[old]
            row.update(
                input_group=name,
                circuit_recipe=recipe,
                source="test/benchmarks/" + source_name,
                provenance_grade="C",
                replaces=old,
                baseline_probe={},
            )
    fixtures = root / "fixtures"
    for d in ("circuits", "targets", "references"):
        (fixtures / d).mkdir(parents=True, exist_ok=True)
    targets, recipes, circuits, artifacts, references = {}, {}, {}, {}, {}
    provenance = []
    target_names = {r["target_recipe"]: r["target_id"] for r in catalog}
    target_names.update(hh9_cx="heavy_hex_d9_cx", hh9_ecr="heavy_hex_d9_ecr")
    for recipe, name in sorted(target_names.items()):
        kwargs, native = probe.build_target(recipe)
        if "target" in kwargs:
            target = kwargs["target"]
            constraints = {"constraint_form": "target"}
        else:
            coupling = kwargs["coupling_map"]
            edges = [
                list(e)
                for e in (coupling.get_edges() if isinstance(coupling, CouplingMap) else coupling)
            ]
            width = max(q for edge in edges for q in edge) + 1
            target = Target.from_configuration(
                kwargs["basis_gates"], width, coupling_map=CouplingMap(edges)
            )
            constraints = dict(
                constraint_form="loose", basis_gates=kwargs["basis_gates"], coupling_map=edges
            )
        data = export_target(target, native)
        file = f"targets/{name}.target.json"
        write_json(fixtures / file, data)
        targets[name] = dict(file=file, sha256=digest(data), native_2q_names=native)
        recipes[name] = constraints

    def persist(name, circuit, folder="circuits", restricted=False):
        data = export_circuit(circuit)
        # Validate the typed input importer before freezing its bytes.
        if export_circuit(import_circuit(data)) != data:
            raise RuntimeError(f"Curation roundtrip failed: {name}")
        file = f"{folder}/{name}.ops.jsonl.gz"
        if restricted:
            with tempfile.TemporaryDirectory() as directory:
                hash_ = write_circuit(
                    Path(directory) / "input.gz", data["header"], data["operations"]
                )
        else:
            hash_ = write_circuit(fixtures / file, data["header"], data["operations"])
        return dict(file=file, sha256=hash_)

    from test.benchmarks import utils as upstream

    original_random_unitary = upstream.random_unitary
    for row in catalog:
        name = row["input_group"]
        if name in circuits:
            continue
        print("curate", name, flush=True)
        matrix_seed = (
            10
            if row["circuit_recipe"].startswith("qv_w")
            else (12345 if name == "qv_n50_d50" else 0)
        )
        generator = np.random.default_rng(matrix_seed)
        # Upstream seeds pairings but omits matrix seeds. Preserve the constructor,
        # inject its matrix RNG explicitly, then freeze the resulting artifact.
        upstream.random_unitary = lambda dim, rng=generator: random_unitary(dim, seed=rng)
        try:
            circuit = probe.build_circuit(row["circuit_recipe"])
        finally:
            upstream.random_unitary = original_random_unitary
        circuits[name] = circuit
        artifacts[name] = persist(name, circuit, restricted=name in RESTRICTED)
        if "trotter" in name:
            reference = circuit.decompose(reps=10)
            references[name] = dict(
                kind="frozen_circuit",
                contract="product_formula",
                **persist(name, reference, folder="references"),
            )
        provenance.append(
            dict(
                input_group=name,
                source=row["source"],
                commit="0131cbbcc",
                qiskit=qiskit.__version__,
                artifact=artifacts[name],
                license="unresolved; artifact omitted"
                if name in RESTRICTED
                else (
                    "BSD-3-Clause (QUEKO) and Apache-2.0 (Qiskit adapter)"
                    if name.startswith("queko")
                    else "Apache-2.0"
                ),
                matrix_seed=matrix_seed if name.startswith("qv_") else None,
                replaces=row.get("replaces"),
            )
        )
    for name in ("single_h", "cancel_2q"):
        circuit = QuantumCircuit(1 if name == "single_h" else 2)
        circuit.h(0)
        if name == "cancel_2q":
            circuit.h(0)
            for _ in range(4):
                circuit.cx(0, 1)
        circuits[name] = circuit
        artifacts[name] = persist(name, circuit)
    rng = random.Random(20260924)
    variants = {}
    for name in ITERATIONS:
        data = export_circuit(circuits[name])
        for op in data["operations"]:
            if op[0] in {"rx", "ry", "rz"}:
                op[3] = [float(rng.choice([-3, -1, 1, 3]) * np.pi / 2).hex()]
        variants[name] = persist(name + "_cv1", import_circuit(data))

    def make_case(row, level, role, prefix="confirm"):
        name, target_id = row["input_group"], row["target_id"]
        symbolic = bool(circuits[name].parameters)
        variant = (
            "sabre_methods"
            if row["probe_id"].endswith("_sabre")
            else "default_methods"
            if name.startswith("queko")
            else "symbolic"
            if symbolic
            else "numeric"
        )
        options = dict(OPTIONS)
        if variant == "sabre_methods":
            options.update(layout_method="sabre", routing_method="sabre")
        active = len(
            {
                circuits[name].find_bit(q).index
                for i in circuits[name].data
                if i.operation.name != "barrier"
                for q in i.qubits
            }
        )
        suffix = "+sabre" if variant == "sabre_methods" else ""
        case = dict(
            case_id=f"{prefix}/{name}{suffix}/{target_id}/L{level}",
            role=role,
            family=row["family"],
            input_group=name,
            variant=variant,
            size_band="small" if active <= 16 else "medium" if active <= 64 else "large",
            logical_qubits=circuits[name].num_qubits,
            active_qubits=active,
            topology=row["topology"],
            native_basis=row["native_basis"],
            optimization_level=level,
            circuit=artifacts[name],
            target=targets[target_id],
            options=options,
            semantic_reference=references.get(name, {"kind": "input"}),
            input_domain="all_zero",
            seeds_per_block=row["seeds"].get(str(level), 100),
            weight=0.0,
            timeout_s=120,
            modes=["quality", "prefix"],
            provenance=dict(
                grade=row["provenance_grade"], source=row["source"], commit="0131cbbcc"
            ),
            **recipes[target_id],
        )
        if role == "canary":
            expected = 300 if name == "su2_circular_n100" else 3
            case["expected"] = dict(D2=expected, N2=expected)
        if name in variants:
            case["clifford_variant"] = variants[name]
        return case

    confirm = [
        make_case(row, int(level), role) for row in catalog for level, role in row["roles"].items()
    ]
    # Level 3 VF2 odd-ring cost guard is deliberately limited to ten seeds.
    iteration = []
    for name in ITERATIONS:
        row = next(r for r in catalog if r["input_group"] == name)
        for basis in ("cz", "cx", "ecr"):
            r = dict(row, target_id="heavy_hex_d9_" + basis, native_basis=basis)
            case = make_case(r, 2, "scored" if basis == "cz" else "guard", "iterations")
            case["panel"] = "primary" if basis == "cz" else "basis-" + basis
            case["weight"] = 1 / 3
            iteration.append(case)
            if basis != "cz":
                confirm.append(
                    dict(
                        case, case_id=case["case_id"].replace("iterations/", "confirm/"), weight=0.0
                    )
                )
    for name, levels in [("su2_circular_n100", [2]), ("long_2q_sequence", [2, 3])]:
        row = next(r for r in catalog if r["input_group"] == name)
        iteration.extend(make_case(row, level, "canary", "iterations") for level in levels)
    timing = []

    def timed(case, id_, seed, mode="timing_e2e", panel="timing"):
        return dict(
            copy.deepcopy(case),
            case_id=id_,
            role="timing",
            panel=panel,
            weight=0.0,
            modes=[mode],
            timing={"fixed_seed": seed},
            seeds_per_block=1,
        )

    template = next(c for c in confirm if c["input_group"] == "qaoa_complete_n8")
    for i, name in enumerate(("single_h", "cancel_2q"), 1):
        c = dict(
            template,
            input_group=name,
            circuit=artifacts[name],
            logical_qubits=i,
            active_qubits=i,
            optimization_level=None,
            target=targets["mumbai_27_loose"],
            semantic_reference={"kind": "input"},
            **recipes["mumbai_27_loose"],
        )
        timing.append(timed(c, f"T{i}", 20220125))
    for name in ("qv_n14_d14", "long_2q_sequence"):
        template = next(c for c in confirm if c["input_group"] == name)
        for level in range(4):
            timing.append(timed(dict(template, optimization_level=level), f"T{len(timing) + 1}", 0))
    for case in iteration[:9]:
        timing.append(timed(case, f"T{len(timing) + 1}", 1234567845, "timing_reuse"))
    for basis in ("cx", "cz", "ecr"):
        case = next(c for c in iteration if c["native_basis"] == basis)
        timing.append(timed(case, "preset/" + basis, 0, "preset_build", "preset"))
    for profile, cases in [("iterations-profile", iteration), ("confirm-profile", confirm)]:
        cases.extend(copy.deepcopy(timing))
        if profile == "confirm-profile":
            heavy = [
                "qft_n100",
                "square_heisenberg_n100",
                "qaoa_ba_n100_3reps",
                "qv_n50_d50",
                "multiplier_h18_n32",
                "bv_all_ones_n100",
                "queko_bss_53",
                "su2_circular_n89",
            ]
            for name in heavy:
                template = next(
                    c for c in cases if c["input_group"] == name and c["role"] == "scored"
                )
                for level in range(3 if name == "su2_circular_n89" else 4):
                    cases.append(
                        timed(
                            dict(template, optimization_level=level),
                            f"confirm-timing/{name}/L{level}",
                            0,
                            panel="confirm-timing",
                        )
                    )
            for name in heavy + ["multiplier_h18_n20"]:
                template = next(
                    c for c in cases if c["input_group"] == name and c["optimization_level"] == 2
                )
                cases.append(
                    dict(
                        copy.deepcopy(template),
                        case_id="memory/" + name,
                        role="memory",
                        panel="memory",
                        weight=0.0,
                        modes=["memory"],
                    )
                )
        weights = hierarchical_weights([c for c in cases if c["role"] == "scored"])
        for c in cases:
            if c["role"] == "scored":
                c["weight"] = weights[c["case_id"]]
        manifest = dict(
            format="qtb-manifest/1",
            profile=profile,
            version=1,
            cases=cases,
            status="unqualified",
            coverage_gaps=[
                "No line target in upstream suite",
                "G2, G3, G6 have one sparse topology class",
                "G7 has no scored level 3",
                "Some family/size cells have fewer than three independent groups",
                "G4, G5, G8 have no large-band scored inputs",
            ],
            unavailable_fixtures=[],
            replacements={k: v[0] for k, v in REPLACEMENTS.items()}
            if profile == "confirm-profile"
            else {},
        )
        directory = root / "profiles" / profile
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "manifest.json", manifest)
        write_json(directory / "policy.json", policy(profile))
    write_json(fixtures / "provenance.json", provenance)
    shutil.copy2(source / "LICENSE.txt", fixtures / "LICENSE-QISKIT.txt")
    lines = [
        "# Fixture provenance",
        "",
        "Curated from Qiskit commit `0131cbbcc` using the pinned Qiskit 2.5.2 importer.",
        "Canonical data is the contract. Comparison runs never call these constructors.",
        "Qiskit-derived artifacts retain the Apache-2.0 license in LICENSE-QISKIT.txt.",
        "QUEKO source: https://github.com/UCLA-VAST/QUEKO-benchmark (BSD-3-Clause).",
        "The six RevLib artifacts with unresolved terms were replaced under plan section 3.8.",
        "Each replacement is a distinct upstream synthesis-library construction; "
        "the manifest records the mapping.",
        "Baseline roles, cost, and oracle coverage still require controlled-runner qualification.",
        "",
        "| Fixture | Upstream source | License / availability |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {p['input_group']} | {p['source']} | {p['license']} |" for p in provenance]
    (fixtures / "PROVENANCE.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT)
    args = parser.parse_args()
    curate(args.source.resolve(), args.output.resolve())
