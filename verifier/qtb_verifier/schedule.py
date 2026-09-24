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
        wires = [("q", q) for q in qs] + [("c", c) for c in cs]
        if not math.isfinite(start) or not math.isfinite(duration):
            errors.append("Non-finite schedule value")
            continue
        if start < 0 or duration < 0:
            errors.append("Negative start or duration")
        if any(start < ends.get(w, 0) for w in wires):
            errors.append("Overlap or dependency violation")
        alignment = constraints["acquire_alignment" if name == "measure" else "pulse_alignment"]
        if name not in {"barrier", "delay"} and start % alignment:
            errors.append("Alignment violation")
        if name not in {"barrier", "delay", "measure"} and duration:
            if duration % constraints["granularity"] or duration < constraints["min_length"]:
                errors.append("Pulse duration violates granularity or minimum length")
        prop = support.get(name, {}).get(tuple(qs), {})
        if prop.get("duration") is not None and dt is not None:
            if abs(duration - round(numeric(prop["duration"]) / dt)) > 1e-8:
                errors.append("Duration differs from target")
        if name == "delay" and any(start != ends.get(w, 0) for w in wires):
            errors.append("Delay does not fill the idle gap")
        for wire in wires:
            ends[wire] = start + duration
    return {
        "status": "mismatch" if errors else "verified",
        "oracle": "C5",
        "errors": errors,
        "makespan": max(ends.values(), default=0),
    }
