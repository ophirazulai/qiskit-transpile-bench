import copy

import pytest

from qtb.canonical import CIRCUIT_FORMAT, circuit_hash, digest, read_circuit, write_circuit
from qtb.errors import HarnessError
from qtb.metrics import StructuralChecker, d2_n2, layout_errors
from qtb.metrics.replay import replay


def op(name, qs, cs=None, params=None, payload=None):
    return [name, qs, cs or [], params or [], payload]


def test_metric_example_and_barrier_synchronization():
    ops = [op("cz", [0, 1]), op("cz", [2, 3]), op("rz", [1]), op("cz", [1, 2]), op("cz", [0, 1])]
    assert d2_n2(ops, ["cz"]) == (3, 4)
    ops = [op("cz", [0, 1]), op("cz", [0, 1]), op("barrier", [1, 2]), op("cz", [2, 3])]
    assert d2_n2(ops, ["cz"]) == (3, 3)
    assert d2_n2([], ["cz"]) == (0, 0)


def test_classical_wire_synchronization():
    assert d2_n2(
        [op("cx", [0, 1]), op("measure", [1], [0]), op("measure", [2], [0]), op("cx", [2, 3])],
        ["cx"],
    ) == (2, 2)
    with pytest.raises(HarnessError):
        d2_n2([op("if_else", [0], [0])], ["cx"])


def test_canonical_gzip_is_stable(tmp_path):
    header = dict(
        format=CIRCUIT_FORMAT,
        num_qubits=2,
        num_clbits=0,
        qregs=[["q", 2]],
        cregs=[],
        global_phase=(0.0).hex(),
        parameters=[],
    )
    ops = [op("rz", [0], params=[(0.2).hex()]), op("cx", [0, 1])]
    a, b = tmp_path / "a.gz", tmp_path / "b.gz"
    assert write_circuit(a, header, ops) == write_circuit(b, header, ops) == circuit_hash(a)
    assert a.read_bytes() == b.read_bytes()
    assert read_circuit(a) == (header, ops)


def layout(initial, final):
    route = [0] * len(initial)
    for i, p in enumerate(initial):
        route[p] = final[i]
    return dict(
        input_num_qubits=len(initial),
        output_num_qubits=len(initial),
        initial_index_layout=initial,
        final_index_layout=final,
        routing_permutation=route,
    )


def test_replay_elided_permutation_and_mutations():
    logical = [op("h", [0]), op("cx", [0, 2])]
    routed = [op("h", [0]), op("swap", [1, 2]), op("cx", [0, 1])]
    mapping = layout([0, 1, 2], [0, 2, 1])
    assert replay(logical, routed, mapping, 3, 3)["status"] == "verified"
    assert replay(logical, routed[:1] + routed[2:], mapping, 3, 3)["status"] == "mismatch"
    broken = copy.deepcopy(mapping)
    broken["final_index_layout"] = [0, 1, 2]
    assert replay(logical, routed, broken, 3, 3)["status"] == "mismatch"
    elided = [1, 2, 0]
    mapped = layout([0, 1, 2], [2, 1, 0])
    assert replay(logical, routed, mapped, 3, 3, elided)["status"] == "verified"


def test_directed_legality():
    target = dict(
        num_qubits=2,
        native_2q_names=["cx"],
        instructions=[dict(name="cx", arity=2, parameters=[], qargs=[[0, 1]])],
    )
    checker = StructuralChecker({"num_qubits": 2, "num_clbits": 0}, target)
    checker.consume(op("cx", [1, 0]))
    assert checker.result()["status"] == "mismatch"
    assert layout_errors(layout([0, 1], [1, 0]), 2, 2, [1, 0])


def test_loose_constraints_allow_implicit_operations_only(tmp_path):
    from qtb.coordinator import structural_result

    target = dict(
        num_qubits=2,
        native_2q_names=["cx"],
        instructions=[dict(name="cx", arity=2, parameters=[], qargs=[[0, 1]])],
    )
    header = dict(
        format=CIRCUIT_FORMAT,
        num_qubits=2,
        num_clbits=1,
        qregs=[["q", 2]],
        cregs=[["c", 1]],
        global_phase=(0.0).hex(),
        parameters=[],
    )
    path = tmp_path / "measured.gz"
    write_circuit(path, header, [op("cx", [0, 1]), op("measure", [1], [0])])
    assert structural_result(path, target, None, 2, constraint_form="loose")["status"] == "verified"
    strict = structural_result(path, target, None, 2, constraint_form="target")
    assert "Unsupported instruction measure" in strict["errors"]

    checker = StructuralChecker(header, target, "loose")
    checker.consume(op("measure", [0]))
    checker.consume(op("h", [0]))
    assert "Wrong classical arity: measure" in checker.result()["errors"]
    assert "Unsupported instruction h" in checker.result()["errors"]


def test_payload_affects_hash():
    assert digest(op("evolution", [0], payload={"hamiltonian": "X"})) != digest(
        op("evolution", [0], payload={"hamiltonian": "Z"})
    )


def test_streamed_union_keeps_idle_logical_positions_and_excludes_barriers(tmp_path):
    from qtb.coordinator import structural_result

    header = dict(
        format=CIRCUIT_FORMAT,
        num_qubits=30,
        num_clbits=0,
        qregs=[["q", 30]],
        cregs=[],
        global_phase=(0.0).hex(),
        parameters=[],
    )
    mapping = layout(list(range(30)), list(range(30)))
    mapping["input_num_qubits"] = 3
    target = dict(
        num_qubits=30,
        native_2q_names=[],
        instructions=[dict(name="x", arity=1, parameters=[], qargs=None)],
    )
    path = tmp_path / "output.gz"
    hash_ = write_circuit(path, header, [op("x", [27]), op("barrier", list(range(30)))])
    result = structural_result(path, target, mapping, 3)
    assert result["status"] == "verified"
    assert result["union_width"] == 4
    assert result["output_hash"] == hash_
