"""C1 and C7: full-width layout-aware unitary equivalence."""

import numpy as np
from qiskit import QuantumCircuit
from qiskit.circuit.library import PermutationGate
from qiskit.quantum_info import Clifford, Operator

from qtb.config import STAGES
from qtb.metrics import layout_errors


def expected_circuit(logical, width, layout):
    errors = layout_errors(layout, logical.num_qubits, width)
    if errors:
        raise ValueError(errors)
    initial = layout["initial_index_layout"] if layout else list(range(width))
    final = layout["final_index_layout"] if layout else list(range(width))
    expected = QuantumCircuit(width, logical.num_clbits)
    expected.compose(logical, qubits=initial[: logical.num_qubits], inplace=True)
    pattern = [0] * width
    for i in range(width):
        pattern[final[i]] = initial[i]
    expected.append(PermutationGate(pattern), range(width))
    return expected


def phase_equal(a, b, rtol=1e-7, atol=1e-8):
    a, b = np.asarray(a), np.asarray(b)
    if a.shape != b.shape:
        return False
    overlap = np.vdot(a.ravel(), b.ravel())
    if abs(overlap) == 0:
        return np.allclose(a, b, rtol=rtol, atol=atol)
    return np.allclose(a * overlap / abs(overlap), b, rtol=rtol, atol=atol)


def verify_unitary(logical, output, layout, contract="all_inputs", max_qubits=10):
    if contract != "all_inputs":
        return {"status": "unverified", "detail": "All-input compile contract required"}
    if output.num_qubits > max_qubits:
        return {"status": "unverified", "detail": "Dense operator width limit"}
    expected = expected_circuit(logical, output.num_qubits, layout)
    a, b = Operator(expected).data, Operator(output).data
    ok = phase_equal(a, b)
    dimension = a.shape[0]
    infidelity = max(0.0, 1 - abs(np.vdot(a.ravel(), b.ravel())) ** 2 / dimension**2)
    return {
        "status": "verified" if ok else "mismatch",
        "oracle": "C1",
        "input_domain": contract,
        "covers": STAGES,
        "substituted": [],
        "process_infidelity": float(infidelity),
    }


def verify_clifford(logical, output, layout, covers=None, substituted=()):
    expected = expected_circuit(logical, output.num_qubits, layout)
    try:
        a, b = Clifford(expected), Clifford(output)
    except Exception as exc:
        return {"status": "unverified", "oracle": "C7", "detail": str(exc)}
    return {
        "status": "verified" if a == b else "mismatch",
        "oracle": "C7",
        "input_domain": "all_inputs",
        "covers": covers or STAGES,
        "substituted": list(substituted),
    }
