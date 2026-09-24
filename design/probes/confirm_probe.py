"""Probe every transpile-benchmark case that exists in test/benchmarks/ at the baseline.

Behind section 3.7 of design/transpilation-benchmark-impl-plan.md.  For each (input,
target) pair and each optimization level 0-3 it records, at the baseline build and in the
serial reference environment: circuit facts, wall time of one transpile() call, and D2/N2
over transpiler seeds 0-2.  Output: confirm_probe.jsonl (one record per compile).
Run from the repository root with the project virtualenv:  python design/probes/confirm_probe.py
"""
import json, os, sys, time, warnings, traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

warnings.filterwarnings("ignore")
os.environ.update({"QISKIT_PARALLEL": "FALSE", "QISKIT_IGNORE_USER_SETTINGS": "TRUE", "RAYON_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"})
os.environ.pop("QISKIT_SABRE_ALL_THREADS", None)
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, ROOT)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "confirm_probe.jsonl")
QASM = os.path.join(ROOT, "test", "benchmarks", "qasm")

SEEDS = [0, 1, 2]
HEAVY = {  # case_id -> seeds per level for expensive cases
    "hwb12": [0],
    "su2_circ_89": [0],
}


def _rochester():
    from test.benchmarks.transpiler_levels import TranspilerLevelBenchmarks
    b = TranspilerLevelBenchmarks(); b.setup(0)
    return b.rochester_coupling_map


def _queko_maps():
    from test.benchmarks.queko import QUEKOTranspilerBench
    b = QUEKOTranspilerBench(); b.setup(0, None)
    return b.tokyo_coupling_map, b.rochester_coupling_map, b.sycamore_coupling_map


def build_target(name):
    """Return dict(target=..., or coupling_map+basis_gates) exactly as upstream configures it."""
    from qiskit.providers.fake_provider import GenericBackendV2
    from qiskit.transpiler import CouplingMap, Target
    from test.benchmarks.legacy_cmaps import MELBOURNE_CMAP, MUMBAI_CMAP
    from qiskit.quantum_info import get_clifford_gate_names
    if name.startswith("hh9_"):
        gate = name.split("_")[1]
        cmap = CouplingMap.from_heavy_hex(9)
        b = GenericBackendV2(cmap.size(), ["rz", "x", "sx", gate, "id"], coupling_map=cmap,
                             control_flow=True, seed=12345678942)
        return {"target": b.target}, [gate]
    if name == "melbourne14":  # transpiler_levels / directional
        b = GenericBackendV2(num_qubits=14, coupling_map=MELBOURNE_CMAP, seed=0)
        return {"target": b.target}, ["cx"]
    if name == "melbourne14_u":  # qft.py / random_circuit_hex.py bare config
        return {"coupling_map": MELBOURNE_CMAP, "basis_gates": ["u1", "u2", "u3", "cx", "id"]}, ["cx"]
    if name == "mumbai27":  # transpiler_qualitative
        b = GenericBackendV2(num_qubits=27, coupling_map=MUMBAI_CMAP, seed=0)
        return {"target": b.target}, ["cx"]
    if name == "mumbai27_loose":  # transpiler_benchmarks / quantum_volume bare config
        return {"coupling_map": MUMBAI_CMAP, "basis_gates": ["id", "rz", "sx", "x", "cx", "reset"]}, ["cx"]
    if name == "rochester53_u":  # transpiler_levels bare config
        return {"coupling_map": _rochester(), "basis_gates": ["u1", "u2", "u3", "cx", "id"]}, ["cx"]
    if name in ("tokyo20", "rochester53_sx", "sycamore54"):  # queko bare configs
        t, r, s = _queko_maps()
        cm = {"tokyo20": t, "rochester53_sx": r, "sycamore54": s}[name]
        return {"coupling_map": cm, "basis_gates": ["id", "rz", "sx", "x", "cx"]}, ["cx"]
    if name.startswith("grid"):  # ripple_adder bare config
        side = int(name[4:].split("_")[0])
        return {"coupling_map": CouplingMap.from_grid(side, side),
                "basis_gates": ["u1", "u2", "u3", "cx", "id"]}, ["cx"]
    if name.startswith("ft_a2a_"):  # transpiler_ft all-to-all Clifford+RZ
        n = int(name.split("_")[-1])
        t = Target.from_configuration(["rz", "measure"] + get_clifford_gate_names(), n)
        return {"target": t}, [g for g in get_clifford_gate_names() if g in ("cx", "cz", "cy", "swap", "iswap", "ecr", "dcx")]
    raise ValueError(name)


def build_circuit(name):
    from qiskit import QuantumCircuit
    from qiskit.circuit.library import efficient_su2
    from test.benchmarks import utils as U
    from test.benchmarks.qft import build_model_circuit
    from test.benchmarks.random_circuit_hex import make_circuit_ring
    from qiskit import QuantumRegister
    import numpy as np
    qasm = {
        "qft_n100": "qft_N100.qasm", "square_heisenberg_n100": "square_heisenberg_N100.qasm",
        "qaoa_ba_n100_3reps": "qaoa_barabasi_albert_N100_3reps.qasm", "hwb12": "hwb12.qasm",
        "long_2q_sequence": "test_eoh_qasm.qasm", "dtc_n100": "dtc_100_cx_12345.qasm",
        "queko_bigd_20": "20QBT_45CYC_.0D1_.1D2_3.qasm", "queko_bss_53": "53QBT_100CYC_QSE_3.qasm",
        "queko_bntf_54": "54QBT_25CYC_QSE_3.qasm", "time_qft_16": "time_qft_16.qasm",
        "depth_4gt10_v1_81": "depth_4gt10-v1_81.qasm", "depth_4mod5_v0_19": "depth_4mod5-v0_19.qasm",
        "depth_mod8_10_178": "depth_mod8-10_178.qasm", "time_cnt3_5_179": "time_cnt3-5_179.qasm",
        "time_cnt3_5_180": "time_cnt3-5_180.qasm",
    }
    if name in qasm:
        return QuantumCircuit.from_qasm_file(os.path.join(QASM, qasm[name]))
    if name == "qv_n50_d50": return U.build_qv_model_circuit(50, 50, 12345)
    if name == "qv_50_x_20": return U.build_qv_model_circuit(50, 20, 0)
    if name == "qv_14_x_14": return U.build_qv_model_circuit(14, 14, 0)
    if name.startswith("qv_w"):  # QuantumVolumeBenchmark widths (frozen with seed 10 here)
        w = int(name[4:]); return U.build_qv_model_circuit(w, w, 10)
    if name == "su2_circ_100": return efficient_su2(100, reps=3, entanglement="circular")
    if name == "su2_circ_89": return efficient_su2(89, reps=3, entanglement="circular")
    if name == "bv_all_ones_n100": return U.bv_all_ones(100)
    if name == "bvlike_n100": return U.trivial_bvlike_circuit(100)
    if name.startswith("bv_all_ones_n"): return U.bv_all_ones(int(name.split("n")[-1]))
    if name.startswith("ripple_"): return U.build_ripple_adder_circuit(int(name.split("_")[1]))
    if name.startswith("qftbm_"): return build_model_circuit(QuantumRegister(int(name.split("_")[1])))
    if name.startswith("ring_"):
        n = int(name.split("_")[1]); return make_circuit_ring(n, 2 * n, 0)[0]
    if name.startswith("ft_"):  # ft_<circuit>_<n>
        _, cname, n = name.split("_", 2) if name.count("_") == 2 else (None, None, None)
        if cname is None:
            parts = name.split("_"); n = parts[-1]; cname = "_".join(parts[1:-1])
        c = U.create_ft_circuit(cname, int(n))
        if c.parameters:  # bind symbolic parameters deterministically (qaoa)
            rng = np.random.default_rng(7)
            c = c.assign_parameters({p: float(rng.uniform(0, np.pi)) for p in c.parameters})
        return c
    raise ValueError(name)


# (case_id, circuit, target, family, band, tier) -- tier A = exact upstream config, B = existing fixture on an existing target
CASES = [
    # G1 QFT
    ("qft_n100", "qft_n100", "hh9_cz", "G1", "large", "A"),
    ("qft_n100_cx", "qft_n100", "hh9_cx", "G1", "large", "A"),
    ("time_qft_16", "time_qft_16", "mumbai27", "G1", "small", "A"),
    ("qftbm_8", "qftbm_8", "melbourne14_u", "G1", "small", "A"),
    ("qftbm_14", "qftbm_14", "melbourne14_u", "G1", "small", "A"),
    ("ft_qft_32", "ft_qft_32", "rochester53_sx", "G1", "medium", "B"),
    ("ft_qft_64", "ft_qft_64", "hh9_cz", "G1", "medium", "B"),
    # G2 Hamiltonian simulation
    ("square_heisenberg_n100", "square_heisenberg_n100", "hh9_cz", "G2", "large", "A"),
    ("dtc_n100", "dtc_n100", "hh9_cz", "G2", "large", "B"),
    ("ft_trotter_16", "ft_trotter_16", "mumbai27", "G2", "small", "B"),
    ("ft_trotter_32", "ft_trotter_32", "rochester53_sx", "G2", "medium", "B"),
    ("ft_trotter_64", "ft_trotter_64", "hh9_cz", "G2", "medium", "B"),
    # G3 QAOA
    ("qaoa_ba_n100_3reps", "qaoa_ba_n100_3reps", "hh9_cz", "G3", "large", "A"),
    ("ft_qaoa_8", "ft_qaoa_8", "mumbai27", "G3", "small", "B"),
    ("ft_qaoa_16", "ft_qaoa_16", "mumbai27", "G3", "small", "B"),
    ("ft_qaoa_32", "ft_qaoa_32", "rochester53_sx", "G3", "medium", "B"),
    # G4 Quantum volume
    ("qv_n50_d50", "qv_n50_d50", "hh9_cz", "G4", "medium", "A"),
    ("qv_50_x_20", "qv_50_x_20", "rochester53_u", "G4", "medium", "A"),
    ("qv_14_x_14", "qv_14_x_14", "melbourne14", "G4", "small", "A"),
    ("qv_w8", "qv_w8", "mumbai27_loose", "G4", "small", "A"),
    ("qv_w14", "qv_w14", "mumbai27_loose", "G4", "small", "A"),
    ("qv_w20", "qv_w20", "mumbai27_loose", "G4", "medium", "A"),
    ("qv_w27", "qv_w27", "mumbai27_loose", "G4", "medium", "A"),
    # G5 Reversible logic
    ("hwb12", "hwb12", "hh9_cz", "G5", "medium", "A"),
    ("ripple_10", "ripple_10", "grid5", "G5", "medium", "A"),
    ("ripple_20", "ripple_20", "grid7", "G5", "medium", "A"),
    ("depth_4gt10_v1_81", "depth_4gt10_v1_81", "mumbai27", "G5", "small", "A"),
    ("depth_4mod5_v0_19", "depth_4mod5_v0_19", "mumbai27", "G5", "small", "A"),
    ("depth_mod8_10_178", "depth_mod8_10_178", "mumbai27", "G5", "small", "A"),
    ("time_cnt3_5_179", "time_cnt3_5_179", "mumbai27", "G5", "small", "A"),
    ("time_cnt3_5_180", "time_cnt3_5_180", "mumbai27", "G5", "small", "A"),
    ("ft_mcx_16", "ft_mcx_16", "mumbai27", "G5", "small", "B"),
    ("ft_modular_adder_16", "ft_modular_adder_16", "mumbai27", "G5", "small", "B"),
    ("ft_multiplier_16", "ft_multiplier_16", "mumbai27", "G5", "small", "B"),
    ("ft_modular_adder_32", "ft_modular_adder_32", "rochester53_sx", "G5", "medium", "B"),
    ("ft_multiplier_32", "ft_multiplier_32", "rochester53_sx", "G5", "medium", "B"),
    ("ft_mcx_64", "ft_mcx_64", "hh9_cz", "G5", "medium", "B"),
    ("ft_modular_adder_64", "ft_modular_adder_64", "hh9_cz", "G5", "medium", "B"),
    # G6 Bernstein-Vazirani
    ("bv_all_ones_n100", "bv_all_ones_n100", "hh9_cz", "G6", "large", "A"),
    ("bvlike_n100", "bvlike_n100", "hh9_cz", "G6", "large", "A"),
    ("bv_all_ones_n16", "bv_all_ones_n16", "mumbai27", "G6", "small", "C"),
    ("bv_all_ones_n50", "bv_all_ones_n50", "rochester53_sx", "G6", "medium", "C"),
    # G7 Variational ansatz
    ("su2_circ_100", "su2_circ_100", "hh9_cz", "G7", "large", "A"),
    ("su2_circ_89", "su2_circ_89", "hh9_cz", "G7", "large", "A"),
    ("ring_8", "ring_8", "melbourne14_u", "G7", "small", "A"),
    ("ring_14", "ring_14", "melbourne14_u", "G7", "small", "A"),
    # G8 Routing challenges (default methods = deterministic guard; sabre = scored)
    ("queko_bigd_20", "queko_bigd_20", "tokyo20", "G8", "medium", "A"),
    ("queko_bss_53", "queko_bss_53", "rochester53_sx", "G8", "medium", "A"),
    ("queko_bntf_54", "queko_bntf_54", "sycamore54", "G8", "medium", "A"),
    ("queko_bigd_20_sabre", "queko_bigd_20", "tokyo20", "G8", "medium", "A"),
    ("queko_bss_53_sabre", "queko_bss_53", "rochester53_sx", "G8", "medium", "A"),
    ("queko_bntf_54_sabre", "queko_bntf_54", "sycamore54", "G8", "medium", "A"),
    # canary
    ("long_2q_sequence", "long_2q_sequence", "rochester53_u", "canary", "tiny", "A"),
    # all-to-all control (transpiler_ft configuration)
    ("ft_a2a_qft_16", "ft_qft_16", "ft_a2a_16", "G1", "small", "A"),
    ("ft_a2a_trotter_16", "ft_trotter_16", "ft_a2a_16", "G2", "small", "A"),
    ("ft_a2a_qaoa_16", "ft_qaoa_16", "ft_a2a_16", "G3", "small", "A"),
    ("ft_a2a_multiplier_16", "ft_multiplier_16", "ft_a2a_16", "G5", "small", "A"),
    ("ft_a2a_modular_adder_16", "ft_modular_adder_16", "ft_a2a_16", "G5", "small", "A"),
    ("ft_a2a_mcx_16", "ft_mcx_16", "ft_a2a_16", "G5", "small", "A"),
]


def circuit_facts(qc):
    from collections import Counter
    ops = Counter()
    n2 = 0
    pairs = set()
    for inst in qc.data:
        ops[inst.operation.name] += 1
        if inst.operation.num_qubits == 2 and inst.operation.name not in ("barrier",):
            n2 += 1
            pairs.add(tuple(sorted(qc.find_bit(q).index for q in inst.qubits)))
    from qiskit import transpile
    ref = transpile(qc, basis_gates=["u3", "cx"], optimization_level=0)
    ref2q = sum(1 for i in ref.data if i.operation.name == "cx")
    return {"n_qubits": qc.num_qubits, "n_gates": qc.size(), "n_2q": n2, "depth": qc.depth(),
            "ref_gates_u3cx": ref.size(), "ref_cx": ref2q, "ref_cx_depth": ref.depth(lambda i: i.operation.name == "cx"),
            "ref_pairs": len({tuple(sorted(ref.find_bit(q).index for q in i.qubits)) for i in ref.data if i.operation.name == "cx"}),
            "depth_2q": qc.depth(lambda i: i.operation.num_qubits == 2), "pairs": len(pairs),
            "ops": dict(ops.most_common(6)), "params": len(qc.parameters)}


def run_one(case_id, circ_name, tgt_name, level, seed):
    warnings.filterwarnings("ignore")
    from qiskit import transpile
    try:
        qc = build_circuit(circ_name)
        kw, native2q = build_target(tgt_name)
        if case_id.endswith("_sabre"):
            kw = dict(kw, layout_method="sabre", routing_method="sabre")
        t0 = time.perf_counter()
        out = transpile(qc, optimization_level=level, seed_transpiler=seed, **kw)
        dt = time.perf_counter() - t0
        native = set(native2q)
        d2 = out.depth(lambda i: i.operation.name in native and i.operation.num_qubits == 2)
        n2 = sum(1 for i in out.data if i.operation.name in native and i.operation.num_qubits == 2)
        return {"case": case_id, "level": level, "seed": seed, "t": round(dt, 3), "D2": d2, "N2": n2,
                "out_qubits": out.num_qubits, "ok": True}
    except Exception as e:  # noqa
        return {"case": case_id, "level": level, "seed": seed, "ok": False,
                "err": f"{type(e).__name__}: {e}"[:300]}


def main():
    only = set(sys.argv[1:])
    done = set()
    if os.path.exists(OUT):
        for line in open(OUT):
            r = json.loads(line)
            if r.get("kind") == "compile":
                done.add((r["case"], r["level"], r["seed"]))
    jobs = []
    facts = {}
    with open(OUT, "a") as f:
        for case_id, circ, tgt, fam, band, tier in CASES:
            if only and case_id not in only:
                continue
            try:
                qc = build_circuit(circ)
                fx = circuit_facts(qc)
            except Exception as e:  # noqa
                f.write(json.dumps({"kind": "facts", "case": case_id, "err": str(e)[:300]}) + "\n")
                continue
            f.write(json.dumps({"kind": "facts", "case": case_id, "circuit": circ, "target": tgt,
                                "family": fam, "band": band, "tier": tier, **fx}) + "\n")
            seeds = HEAVY.get(case_id, SEEDS)
            for level in range(4):
                for seed in seeds:
                    if (case_id, level, seed) not in done:
                        jobs.append((case_id, circ, tgt, level, seed))
    print(f"{len(jobs)} compiles to run", flush=True)
    # heavy first so they don't straggle
    order = {"hwb12": 0, "su2_circ_89": 1}
    jobs.sort(key=lambda j: order.get(j[0], 5))
    with ProcessPoolExecutor(max_workers=6) as ex, open(OUT, "a") as f:
        futs = {ex.submit(run_one, *j): j for j in jobs}
        for i, fut in enumerate(as_completed(futs)):
            r = fut.result()
            r["kind"] = "compile"
            f.write(json.dumps(r) + "\n"); f.flush()
            if i % 25 == 0:
                print(f"[{i+1}/{len(jobs)}] {r['case']} L{r['level']} s{r['seed']} -> "
                      f"{r.get('D2')}/{r.get('N2')} {r.get('t')}s {r.get('err','')}", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
