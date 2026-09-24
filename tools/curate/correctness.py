"""Freeze harness-owned C1–C5 regression fixtures in the pinned verifier environment."""

from pathlib import Path

import numpy as np
from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister
from qiskit.circuit import Parameter
from qiskit.providers.fake_provider import GenericBackendV2
from qiskit.quantum_info import random_unitary
from qiskit.transpiler import CouplingMap

from qtb.canonical import digest, write_circuit, write_json
from qtb_worker.adapter import export_circuit, export_target

ROOT = Path(__file__).resolve().parents[2]


def main():
    fixtures = ROOT / "fixtures"
    cases = []
    circuits = {}
    for n in range(1, 7):
        c = QuantumCircuit(n)
        c.h(0)
        c.h(0)
        c.ry(0.71, 0)
        c.rz(1e-4, 0)
        c.rx(1e-9, 0)
        c.unitary(random_unitary(2 ** min(n, 3), seed=100 + n), list(range(min(n, 3))))
        if n > 1:
            c.cx(0, n - 1)
            c.rz(0.3, n - 1)
            c.cx(0, n - 1)
        if n > 2:
            c.ccx(0, n - 1, 1)
            c.swap(0, 1)
        circuits[f"c1_n{n}"] = ("C1", c)
    q = QuantumRegister(4, "q")
    a = ClassicalRegister(2, "a")
    b = ClassicalRegister(1, "b")
    c = QuantumCircuit(q, a, b)
    c.ry(0.5, 0)
    c.ry(1.2, 3)
    c.cx(0, 3)
    c.cx(3, 1)
    c.x(2)
    c.measure(q[3], b[0])
    c.measure(q[0], a[1])
    c.measure(q[2], a[0])
    circuits["c2_scrambled_partial"] = ("C2", c)
    c = QuantumCircuit(3, 1)
    c.h(0)
    c.measure(0, 0)
    with c.if_test((c.clbits[0], 1)):
        c.x(2)
    c.reset(0)
    with c.for_loop(range(2)):
        c.cx(1, 2)
    circuits["c3_branch_reset_loop"] = ("C3", c)
    p = Parameter("theta")
    c = QuantumCircuit(3)
    c.ry(p, 0)
    c.cx(0, 2)
    c.rz(p.sin() + p / 3, 2)
    circuits["c4_symbolic"] = ("C4", c)
    c = QuantumCircuit(3)
    c.x(0)
    c.cx(0, 1)
    c.x(2)
    c.measure_all()
    circuits["c5_schedule"] = ("C5", c)
    for name, (oracle, circuit) in circuits.items():
        data = export_circuit(circuit)
        file = f"circuits/{name}.ops.jsonl.gz"
        artifact = dict(
            file=file, sha256=write_circuit(fixtures / file, data["header"], data["operations"])
        )
        bindings = [{"theta": v} for v in (0.0, np.pi / 2, np.pi, -0.4)] if oracle == "C4" else []
        refs = []
        for i, binding in enumerate(bindings):
            bound = circuit.assign_parameters({p: binding[p.name] for p in circuit.parameters})
            data = export_circuit(bound)
            file = f"references/{name}_binding{i}.ops.jsonl.gz"
            refs.append(
                dict(
                    file=file,
                    sha256=write_circuit(fixtures / file, data["header"], data["operations"]),
                )
            )
        for width in sorted({circuit.num_qubits, circuit.num_qubits + 2}):
            for basis in ("cx", "cz", "ecr"):
                target = GenericBackendV2(
                    width,
                    basis_gates=["id", "rz", "sx", "x"] + ([basis] if width > 1 else []),
                    coupling_map=CouplingMap.from_line(width) if width > 1 else None,
                    control_flow=True,
                    seed=17,
                ).target
                data = export_target(target, [basis])
                file = f"targets/check_line_{width}_{basis}.target.json"
                write_json(fixtures / file, data)
                target_art = dict(file=file, sha256=digest(data), native_2q_names=[basis])
                for level in range(4):
                    for explicit in (False, True):
                        options = dict(
                            approximation_degree=1.0,
                            qubits_initially_zero=oracle not in ("C1", "C4"),
                            initial_layout=list(reversed(range(width)))[: circuit.num_qubits]
                            if explicit
                            else None,
                            layout_method=None,
                            routing_method=None,
                            translation_method=None,
                            scheduling_method="asap" if oracle == "C5" else None,
                        )
                        cases.append(
                            dict(
                                case_id=f"checks/{name}/{width}/{basis}/L{level}/{explicit}",
                                role="guard",
                                family="correctness",
                                size_band="small",
                                topology="line",
                                native_basis=basis,
                                input_group=name,
                                variant="numeric",
                                optimization_level=level,
                                logical_qubits=circuit.num_qubits,
                                active_qubits=circuit.num_qubits,
                                seeds_per_block=5,
                                circuit=artifact,
                                target=target_art,
                                semantic_reference={"kind": "input"},
                                input_domain="all_inputs" if oracle in ("C1", "C4") else "all_zero",
                                constraint_form="target",
                                options=options,
                                weight=0.0,
                                timeout_s=120,
                                modes=["quality"],
                                oracle=oracle,
                                bindings=bindings,
                                binding_references=refs,
                            )
                        )
    write_json(fixtures / "correctness-suite.json", dict(format="qtb-correctness/1", cases=cases))


if __name__ == "__main__":
    main()
