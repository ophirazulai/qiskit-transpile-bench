"""C5 schedule validation independent of the revision's timing helpers."""

import math

from qtb.canonical import numeric


def verify_schedule(operations, starts, durations, target):
    errors, ends = [], {}
    if len(operations) != len(starts) or len(starts) != len(durations):
        return {"status": "mismatch", "errors": ["Missing schedule entries"]}
    constraints = target["timing_constraints"]
    support = {
        spec["name"]: {tuple(p["qargs"]): p for p in spec["properties"] if p["qargs"] is not None}
        for spec in target["instructions"]
    }
    dt = numeric(target["dt"]) if target["dt"] else None
    for op, start, duration in zip(operations, starts, durations, strict=True):
        name, qs, cs, _params, _payload = op
        quantum_wires = [("q", q) for q in qs]
        wires = quantum_wires + [("c", c) for c in cs]
        if not math.isfinite(start) or not math.isfinite(duration):
            errors.append("Non-finite schedule value")
            continue
        if start < 0 or duration < 0:
            errors.append("Negative start or duration")
        if any(start < ends.get(w, 0) for w in wires):
            errors.append("Overlap or dependency violation")
        # The verifier infers gate durations from the frozen target. A later
        # start that exceeds the preceding target-derived end must be covered
        # by an explicit delay on that qubit; otherwise the start-time list
        # has no duration witness for the idle interval.
        if any(start > ends.get(w, 0) for w in quantum_wires):
            errors.append("Idle gap without delay")
        alignment = constraints["acquire_alignment" if name == "measure" else "pulse_alignment"]
        if name not in {"barrier", "delay"} and start % alignment:
            errors.append("Alignment violation")
        if name not in {"barrier", "delay", "measure"} and duration:
            if duration % constraints["granularity"] or duration < constraints["min_length"]:
                errors.append("Pulse duration violates granularity or minimum length")
        prop = support.get(name, {}).get(tuple(qs), {})
        occupied_duration = duration
        if prop.get("duration") is not None and dt is not None:
            occupied_duration = round(numeric(prop["duration"]) / dt)
            if abs(duration - occupied_duration) > 1e-8:
                errors.append("Duration input differs from target")
        for wire in wires:
            ends[wire] = start + occupied_duration
    return {
        "status": "mismatch" if errors else "verified",
        "oracle": "C5",
        "errors": errors,
        "makespan": max(ends.values(), default=0),
    }
