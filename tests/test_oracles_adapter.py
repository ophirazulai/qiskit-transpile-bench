import copy

import pytest
from qiskit import QuantumCircuit, transpile
from qiskit.circuit import AnnotatedOperation, ControlModifier, Parameter, PowerModifier
from qiskit.circuit.library import MCMTGate, PauliEvolutionGate, RYGate, XGate
from qiskit.providers.fake_provider import GenericBackendV2
from qiskit.quantum_info import Operator, SparsePauliOp, random_unitary
from qiskit.synthesis import LieTrotter
from qiskit.transpiler import CouplingMap

from qtb.canonical import digest
from qtb.metrics import d2_n2
from qtb_verifier.dynamic import verify_dynamic
from qtb_verifier.layout_semantics import verify_zero
from qtb_verifier.small_exact import verify_unitary
from qtb_worker.adapter import (
    export_circuit,
    export_layout,
    export_target,
    import_circuit,
    import_target,
)


def test_typed_operations_survive_roundtrip_and_hash_changes():
    a = QuantumCircuit(3)
    a.append(
        PauliEvolutionGate(
            SparsePauliOp(["XXI", "IZZ"], [0.3, 0.5]),
            time=0.7,
            synthesis=LieTrotter(reps=2, preserve_order=False),
        ),
        range(3),
    )
    a.append(
        AnnotatedOperation(RYGate(0.4), [ControlModifier(2, ctrl_state=1), PowerModifier(0.5)]),
        range(3),
    )
    a.append(MCMTGate(XGate(), 1, 2, ctrl_state=0), range(3))
    a.unitary(random_unitary(8, seed=7), range(3))
    data = export_circuit(a)
    restored = import_circuit(data)
    assert export_circuit(restored) == data
    assert type(restored.data[0].operation).__name__ == "PauliEvolutionGate"
    mutation = copy.deepcopy(data)
    mutation["operations"][0][4]["groups"][0][0][1] = (0.4).hex()
    assert digest(mutation) != digest(data)
    assert export_circuit(import_circuit(mutation)) == mutation


def test_symbolic_expression_and_delay_roundtrip():
    p, q = Parameter("alpha"), Parameter("beta")
    circuit = QuantumCircuit(2)
    circuit.global_phase = p / 3
    circuit.rx(2 * p + q.sin(), 0)
    circuit.rz(p * q**2, 1)
    circuit.delay(24, 0, unit="ns")
    data = export_circuit(circuit)
    assert export_circuit(import_circuit(data)) == data
    opaque = export_circuit(circuit, opaque=True)
    assert opaque["operations"][0][3][0]["kind"] == "symbolic/1"


def test_target_roundtrip_preserves_properties_and_order():
    backend = GenericBackendV2(4, coupling_map=CouplingMap.from_line(4), control_flow=True, seed=0)
    data = export_target(backend.target, ["cx"])
    assert export_target(import_target(data), ["cx"]) == data


@pytest.mark.parametrize("level", range(4))
def test_full_width_all_input_oracle_and_independent_metrics(level):
    circuit = QuantumCircuit(3)
    circuit.h(0)
    circuit.cx(0, 2)
    circuit.ry(0.3, 1)
    circuit.swap(0, 1)
    target = GenericBackendV2(5, coupling_map=CouplingMap.from_line(5), seed=1).target
    out = transpile(
        circuit,
        target=target,
        optimization_level=level,
        initial_layout=[4, 0, 2],
        seed_transpiler=4,
        qubits_initially_zero=False,
    )
    layout = export_layout(out, 3)
    assert verify_unitary(circuit, out, layout)["status"] == "verified"
    ops = export_circuit(out)["operations"]
    d2, n2 = d2_n2(ops, ["cx"])
    assert d2 == out.depth(lambda i: i.operation.name == "cx")
    assert n2 == sum(i.operation.name == "cx" for i in out.data)
    out.x(0)
    assert verify_unitary(circuit, out, layout)["status"] == "mismatch"


def test_zero_contract_rejects_false_unitary_claim():
    circuit = QuantumCircuit(2)
    circuit.cx(0, 1)
    out = transpile(
        circuit, basis_gates=["u", "cx"], optimization_level=2, qubits_initially_zero=True
    )
    assert verify_zero(circuit, out, export_layout(out, 2))["status"] == "verified"
    assert (
        verify_unitary(circuit, out, export_layout(out, 2), contract="all_zero")["status"]
        == "unverified"
    )


def test_terminal_measurement_ignores_unobservable_phase_but_checks_residual_state():
    a = QuantumCircuit(2, 1)
    a.h(0)
    a.ry(0.7, 1)
    a.measure(0, 0)
    b = QuantumCircuit(2, 1)
    b.h(0)
    b.z(0)
    b.ry(0.7, 1)
    b.measure(0, 0)
    assert verify_zero(a, b, None)["status"] == "verified"
    b.x(1)
    assert verify_zero(a, b, None)["status"] == "mismatch"


def test_swapped_measurement_destinations_rejected():
    a = QuantumCircuit(2, 2)
    a.x(0)
    a.measure([0, 1], [0, 1])
    b = QuantumCircuit(2, 2)
    b.x(0)
    b.measure([0, 1], [1, 0])
    assert verify_zero(a, b, None)["status"] == "mismatch"


def test_trotter_reference_is_product_formula_not_exponential():
    gate = PauliEvolutionGate(
        SparsePauliOp(["X", "Z"], [1.0, 1.0]), time=0.8, synthesis=LieTrotter()
    )
    a = QuantumCircuit(1)
    a.append(gate, [0])
    reference = a.decompose(reps=5)
    output = transpile(a, basis_gates=["u"], optimization_level=0)
    assert verify_zero(reference, output, None)["status"] == "verified"
    wrong = QuantumCircuit(1)
    wrong.unitary(Operator(gate), [0])
    assert verify_zero(reference, wrong, None)["status"] == "mismatch"


def test_dynamic_branch_and_reset_mutations():
    a = QuantumCircuit(2, 1)
    a.h(0)
    a.measure(0, 0)
    with a.if_test((a.clbits[0], 1)):
        a.x(1)
    a.reset(0)
    b = import_circuit(export_circuit(a))
    assert verify_dynamic(a, b)["status"] == "verified"
    b.x(1)
    assert verify_dynamic(a, b)["status"] == "mismatch"


def test_coherent_control_exposes_global_phase_error():
    a=QuantumCircuit(1);a.h(0)
    b=a.copy();b.global_phase=.2
    assert verify_unitary(a,b,None)['status']=='verified'
    assert verify_unitary(a,b,None,controlled=True)['status']=='mismatch'
