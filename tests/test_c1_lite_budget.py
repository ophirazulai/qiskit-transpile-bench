"""C1-lite retains measurement semantics without full tensor copies or OOM."""

import numpy as np
from qiskit import QuantumCircuit

from qtb_verifier import layout_semantics


def test_terminal_measurement_branches_are_views_with_correct_bit_order():
    circuit = QuantumCircuit(3, 2)
    circuit.h(0)
    circuit.x(2)
    circuit.measure(2, 0)
    circuit.measure(0, 1)

    branches, remaining = layout_semantics.terminal_branches(circuit)
    assert remaining == [1]
    assert set(branches) == {1, 3}
    assert all(not vector.flags.owndata for vector in branches.values())
    assert all(np.isclose(np.vdot(vector, vector).real, 0.5) for vector in branches.values())
    assert layout_semantics.measurement_distance(circuit, circuit) == (0.0, 0.0)


def test_state_and_branch_budgets_return_unverified_before_statevector(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Statevector must not be allocated")

    monkeypatch.setattr(layout_semantics, "Statevector", unexpected)
    wide = QuantumCircuit(26)
    result = layout_semantics.verify_zero(wide, wide, None, max_qubits=26)
    assert result["status"] == "unverified"
    assert result["detail"] == "Statevector memory budget exceeded"

    fully_measured = QuantumCircuit(16, 16)
    fully_measured.measure(range(16), range(16))
    result = layout_semantics.verify_zero(fully_measured, fully_measured, None)
    assert result["status"] == "unverified"
    assert result["detail"] == "Terminal measurement branch budget exceeded"


def test_declared_25_wire_limit_remains_within_memory_budget(monkeypatch):
    calls = []

    class SmallStatevector:
        def __init__(self, circuit):
            calls.append(circuit.num_qubits)
            self.data = np.array([1.0 + 0.0j])

    monkeypatch.setattr(layout_semantics, "Statevector", SmallStatevector)
    circuit = QuantumCircuit(25)
    assert layout_semantics.verify_zero(circuit, circuit, None)["status"] == "verified"
    assert calls == [25, 25]
