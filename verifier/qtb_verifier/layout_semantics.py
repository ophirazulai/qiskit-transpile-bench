"""C2/C1-lite: zero-input states and terminal measurement instruments."""

import math

import numpy as np
from qiskit import QuantumCircuit
from qiskit.quantum_info import Statevector

from qtb.config import STAGES
from qtb.errors import Unsupported
from qtb.metrics import layout_errors
from qtb_verifier.small_exact import phase_equal

MAX_ESTIMATED_STATE_BYTES = 2 << 30
MAX_MEASURED_BRANCH_WIRES = 15


class MeasurementMismatch(Exception):
    """A supported terminal instrument measures a different set of wires."""


def zero_reference(logical, width, layout):
    errors = layout_errors(layout, logical.num_qubits, width)
    if errors:
        raise ValueError(errors)
    final = layout["final_index_layout"] if layout else list(range(width))
    expected = QuantumCircuit(width, logical.num_clbits)
    expected.compose(logical, qubits=final[: logical.num_qubits], inplace=True)
    return expected


def compact_union(a, b, required=()):
    """Drop only jointly idle wires, preserving all declared logical positions."""
    active = set(required)
    for circuit in (a, b):
        active.update(
            circuit.find_bit(q).index
            for inst in circuit.data
            if inst.operation.name != "barrier"
            for q in inst.qubits
        )
    wires = sorted(active)
    indices = {q: i for i, q in enumerate(wires)}
    circuits = []
    for circuit in (a, b):
        small = QuantumCircuit(len(wires), circuit.num_clbits)
        small.global_phase = circuit.global_phase
        for inst in circuit.data:
            if inst.operation.name == "barrier":
                continue
            small.append(
                inst.operation,
                [indices[circuit.find_bit(q).index] for q in inst.qubits],
                [circuit.find_bit(c).index for c in inst.clbits],
            )
        circuits.append(small)
    return circuits, wires


def terminal_branches(circuit):
    prefix = QuantumCircuit(circuit.num_qubits)
    prefix.global_phase = circuit.global_phase
    measured, destinations = {}, set()
    for inst in circuit.data:
        name = inst.operation.name
        qs = [circuit.find_bit(q).index for q in inst.qubits]
        cs = [circuit.find_bit(c).index for c in inst.clbits]
        if name == "barrier":
            continue
        if name == "measure":
            if qs[0] in measured or cs[0] in destinations:
                raise Unsupported("Repeated terminal measurement")
            measured[qs[0]] = cs[0]
            destinations.add(cs[0])
        elif name in {"reset", "if_else", "for_loop", "while_loop", "switch_case"} or cs:
            raise Unsupported("Not a terminal-measurement circuit")
        elif set(qs) & measured.keys():
            raise Unsupported("Operation after measurement on the same wire")
        else:
            prefix.append(inst.operation, qs)
    if len(measured) > MAX_MEASURED_BRANCH_WIRES:
        raise Unsupported("Terminal measurement branch budget exceeded")
    state = Statevector(prefix).data
    remaining = [q for q in range(circuit.num_qubits) if q not in measured]
    # Basic indexing yields a view for each branch. A transpose followed by
    # reshape can copy the full 2^n complex state for every circuit.
    measured_wires = sorted(measured)
    tensor = state.reshape([2] * circuit.num_qubits)
    branches = {}
    for outcome in range(1 << len(measured_wires)):
        bits = sum(((outcome >> i) & 1) << measured[q] for i, q in enumerate(measured_wires))
        index = [slice(None)] * circuit.num_qubits
        for i, q in enumerate(measured_wires):
            index[circuit.num_qubits - 1 - q] = (outcome >> i) & 1
        vector = tensor[tuple(index)]
        if np.vdot(vector, vector).real > 1e-30:
            branches[bits] = vector
    return branches, remaining


def measurement_distance(a, b):
    """Trace distance of subnormalized rank-one blocks, independent phase per outcome."""
    left, wires_a = terminal_branches(a)
    right, wires_b = terminal_branches(b)
    if wires_a != wires_b:
        raise MeasurementMismatch("Different exposed residual wire sets")
    tvd = distance = 0.0
    for outcome in left.keys() | right.keys():
        x, y = left.get(outcome), right.get(outcome)
        p = float(np.vdot(x, x).real) if x is not None else 0.0
        q = float(np.vdot(y, y).real) if y is not None else 0.0
        tvd += abs(p - q) / 2
        # Avoid subtracting nearly equal O(1) squares: that would turn exact
        # equivalence into an artificial O(sqrt(epsilon)) trace distance.
        orthogonal = 0.0
        if p and q:
            residual = y - x * (np.vdot(x, y) / p)
            orthogonal = float(np.vdot(residual, residual).real)
        distance += math.sqrt((p - q) ** 2 + 4 * p * orthogonal) / 2
    return tvd, distance


def verify_zero(logical, output, layout, max_qubits=25):
    if [(r.name, len(r)) for r in logical.cregs] != [(r.name, len(r)) for r in output.cregs]:
        return {"status": "mismatch", "oracle": "C2", "detail": "Classical registers changed"}
    expected = zero_reference(logical, output.num_qubits, layout)
    final = layout["final_index_layout"] if layout else list(range(output.num_qubits))
    initial = layout["initial_index_layout"] if layout else list(range(output.num_qubits))
    (a, b), wires = compact_union(
        expected, output, final[: logical.num_qubits] + initial[: logical.num_qubits]
    )
    result = {
        "oracle": "C1-lite",
        "input_domain": "all_zero",
        "covers": STAGES,
        "substituted": [],
        "union_width": len(wires),
    }
    if len(wires) > max_qubits:
        return {**result, "status": "unverified", "detail": "Union width exceeds limit"}
    # Two statevectors plus simulation/comparison workspaces can require about
    # four complex128 vectors. Refuse work that exceeds a fixed 2 GiB budget
    # rather than letting the verifier time out or exhaust the runner's memory.
    estimated_bytes = 4 * (1 << len(wires)) * np.dtype(np.complex128).itemsize
    if estimated_bytes > MAX_ESTIMATED_STATE_BYTES:
        return {**result, "status": "unverified", "detail": "Statevector memory budget exceeded"}
    try:
        if any(i.operation.name == "measure" for c in (a, b) for i in c.data):
            tvd, distance = measurement_distance(a, b)
            ok = tvd < 1e-8 and distance < 1e-8
            result.update(TVD=tvd, joint_trace_distance=distance)
        else:
            ok = phase_equal(Statevector(a).data, Statevector(b).data, rtol=0, atol=1e-8)
    except MeasurementMismatch as exc:
        return {**result, "status": "mismatch", "detail": str(exc)}
    except Unsupported as exc:
        return {**result, "status": "unverified", "detail": str(exc)}
    return {**result, "status": "verified" if ok else "mismatch"}
