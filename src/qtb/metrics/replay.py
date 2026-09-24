"""Exact C6 routing replay with elided init permutations."""

from collections import defaultdict, deque

from qtb.canonical import canonical_bytes
from qtb.metrics import CONTROL_FLOW, layout_errors


def replay(logical, routed, layout, input_width, output_width, elided=None):
    errors = layout_errors(layout, input_width, output_width)
    if errors:
        return {"status": "mismatch", "detail": errors}
    initial = layout["initial_index_layout"] if layout else list(range(output_width))
    final = layout["final_index_layout"] if layout else list(range(output_width))
    elided = list(elided or range(input_width)) + list(range(input_width, output_width))
    if sorted(elided) != list(range(output_width)):
        return {"status": "mismatch", "detail": "Invalid elided permutation"}
    ops, queues, signatures = [], defaultdict(deque), set()
    for op in logical:
        name, qs, cs, params, payload = op
        if name == "barrier":
            continue
        if name in CONTROL_FLOW or not qs and not cs:
            return {"status": "unverified", "detail": "Outside static replay model"}
        index = len(ops)
        ops.append(op)
        signatures.add(canonical_bytes([name, params, payload]))
        for wire in [("q", q) for q in qs] + [("c", c) for c in cs]:
            queues[wire].append(index)
    p2v = [0] * output_width
    for v, p in enumerate(initial):
        p2v[p] = v
    swaps = 0
    for name, ps, cs, params, payload in routed:
        if name == "barrier":
            continue
        if any(type(p) is not int or not 0 <= p < output_width for p in ps):
            return {"status": "mismatch", "detail": "Invalid routed wire"}
        vs = [p2v[p] for p in ps]
        wires = [("q", v) for v in vs] + [("c", c) for c in cs]
        if name in CONTROL_FLOW or not wires:
            return {"status": "unverified", "detail": "Outside static replay model"}
        heads = [queues[w][0] if queues[w] else None for w in wires]
        if (
            heads[0] is not None
            and len(set(heads)) == 1
            and ops[heads[0]] == [name, vs, cs, params, payload]
        ):
            for wire in wires:
                queues[wire].popleft()
        elif name == "swap" and len(ps) == 2 and not cs and not params and payload is None:
            a, b = ps
            p2v[a], p2v[b] = p2v[b], p2v[a]
            swaps += 1
        else:
            status = (
                "mismatch"
                if canonical_bytes([name, params, payload]) in signatures
                else "unverified"
            )
            return {"status": status, "detail": "Operation cannot be replayed", "operation": name}
    ok = not any(queues.values()) and all(p2v[final[v]] == elided[v] for v in range(output_width))
    return {
        "status": "verified" if ok else "mismatch",
        "routing_swaps": swaps,
        "covers": ["layout", "routing"],
        "substituted": [],
    }
