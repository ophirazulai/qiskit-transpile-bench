"""C3: exact branching simulation with bounded loops (no shot statistics)."""

import numpy as np
from qiskit.quantum_info import Statevector

from qtb.errors import Unsupported


def simulate(circuit, max_steps=1000):
    if circuit.num_qubits > 8:
        raise Unsupported("Dynamic oracle width exceeds eight qubits")
    initial = np.zeros(2**circuit.num_qubits, complex)
    initial[0] = 1
    steps = 0

    def project(state, wire, bit):
        result = state.copy()
        result[((np.arange(len(state)) >> wire) & 1) != bit] = 0
        return result

    def run(data, branches, qmap, cmap):
        nonlocal steps
        for inst in data.data:
            steps += 1
            if steps > max_steps:
                raise Unsupported("Dynamic execution exceeds frozen loop bound")
            op = inst.operation
            qs = [qmap[data.find_bit(q).index] for q in inst.qubits]
            cs = [cmap[data.find_bit(c).index] for c in inst.clbits]
            if op.name == "barrier":
                continue
            following = []
            for state, classical in branches:
                if op.name in {"measure", "reset"}:
                    for bit in (0, 1):
                        part = project(state, qs[0], bit)
                        if np.vdot(part, part).real <= 1e-30:
                            continue
                        new_classical = classical
                        if op.name == "measure":
                            new_classical = (classical & ~(1 << cs[0])) | (bit << cs[0])
                        elif bit:
                            part = part[np.arange(len(part)) ^ (1 << qs[0])]
                        following.append((part, new_classical))
                elif op.name in {"if_else", "while_loop"}:
                    bits, value = op.condition
                    try:
                        indices = [cmap[data.find_bit(bit).index] for bit in bits]
                    except TypeError:
                        indices = [cmap[data.find_bit(bits).index]]
                    condition = (
                        sum(((classical >> b) & 1) << i for i, b in enumerate(indices)) == value
                    )
                    if op.name == "if_else":
                        block = (
                            op.blocks[0]
                            if condition
                            else (op.blocks[1] if len(op.blocks) == 2 else None)
                        )
                        following.extend(
                            run(block, [(state, classical)], qs, cs)
                            if block
                            else [(state, classical)]
                        )
                    elif condition:
                        body = run(op.blocks[0], [(state, classical)], qs, cs)
                        # Re-run the loop instruction with the updated branches.
                        loop = data.copy_empty_like()
                        loop.append(inst)
                        following.extend(run(loop, body, qmap, cmap))
                    else:
                        following.append((state, classical))
                elif op.name == "for_loop":
                    indices, parameter, block = op.params
                    parts = [(state, classical)]
                    for index in indices:
                        body = block.assign_parameters({parameter: index}) if parameter else block
                        parts = run(body, parts, qs, cs)
                    following.extend(parts)
                else:
                    try:
                        following.append((Statevector(state).evolve(op, qargs=qs).data, classical))
                    except Exception as exc:
                        raise Unsupported(f"Dynamic operation unsupported: {op.name}") from exc
            branches = following
        return branches

    branches = run(
        circuit, [(initial, 0)], list(range(circuit.num_qubits)), list(range(circuit.num_clbits))
    )
    densities = {}
    for state, outcome in branches:
        rho = np.outer(state, state.conj())
        densities[outcome] = densities.get(outcome, 0) + rho
    return densities


def verify_dynamic(reference, output):
    if (reference.num_qubits, reference.num_clbits) != (output.num_qubits, output.num_clbits):
        return {
            "status": "mismatch",
            "oracle": "C3",
            "input_domain": "all_zero",
            "detail": "Quantum or classical output width changed",
        }
    a, b = simulate(reference), simulate(output)
    distance = sum(
        np.abs(np.linalg.eigvalsh(a.get(key, 0) - b.get(key, 0))).sum() / 2
        for key in a.keys() | b.keys()
    )
    return {
        "status": "verified" if distance < 1e-8 else "mismatch",
        "oracle": "C3",
        "input_domain": "all_zero",
        "joint_trace_distance": float(distance),
    }
