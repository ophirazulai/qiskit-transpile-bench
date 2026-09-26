"""``decide``: merge committed stage evidence into the session's verdict and report.

It can run any time after ``compile`` created the session, as often as you like, also after
``clean``. It reads only committed evidence (``complete`` stages, and the crash record of a
``failed`` one) from a consistent snapshot of the stage states, under the shared lifecycle
lock. It writes ``evidence.json``, ``decision.json``, ``report.md`` and ``progress.log`` and
never rewrites ``run.json``.

Required stages are conditional: ``compile`` and ``quality`` always; ``correctness`` when the
gate is open; ``cost`` when the gate is open and correctness found no failure; ``unit-tests``
once it has started. A required stage that has not finished gives ``INCONCLUSIVE``; a failed
stage gives ``ERROR`` through its ``harness/error/<stage>`` record. The verdict function is
unchanged; these rules are applied around it.

A ``noisy`` stage is unfinished: its last invocation was discarded because interference was
detected. The report says so, separately from any observed cost regression. Replayed cost
bundles must satisfy the session's ``cost_evidence`` requirement, also after ``clean``; the
report labels which kind of cost evidence the session holds.
"""

import hashlib
import json
from pathlib import Path

from qtb.canonical import atomic_bytes, digest, read_json, write_json
from qtb.config import data_root, implementation_identity
from qtb.coordinator import profile_prefix, stage_coverage
from qtb.coordinator.runlog import duration
from qtb.coordinator.stages import (
    FINAL,
    STAGE_ORDER,
    baseline_correctness_key,
    cost_due,
    stage_path,
    state_hash,
)
from qtb.coordinator.storage import locked, read_records
from qtb.coordinator.store import Store
from qtb.errors import HarnessError, Precondition
from qtb.evaluator import EXIT_CODES, evaluate_quality
from qtb.reporter import make_decision, write_report


def _archived_profile(root, run):
    manifest = read_json(root / "manifest.json")
    policy = read_json(root / "policy.json")
    if manifest.get("format") != "qtb-manifest/1" or policy.get("format") != "qtb-policy/1":
        raise HarnessError("Unknown archived profile format")
    for name, value in (("manifest", manifest), ("policy", policy)):
        if run["hashes"][name] != digest(value):
            raise HarnessError(f"Archived {name} hash mismatch")
    return manifest, policy


def _snapshot(root):
    """Read one stable view even while shared-lock stage writers finish or retry."""

    def reject_number(value):
        raise HarnessError(f"Invalid stage state number: {value}")

    while True:
        states, hashes = {}, {}
        for stage in STAGE_ORDER:
            path = stage_path(root, stage, "state.json")
            try:
                content = path.read_bytes()
            except FileNotFoundError:
                states[stage], hashes[stage] = None, None
                continue
            states[stage] = json.loads(content, parse_constant=reject_number)
            hashes[stage] = hashlib.sha256(content).hexdigest()
        evidence = {}
        for stage, state in states.items():
            if state is None or state["status"] not in {"complete", "failed"}:
                evidence[stage] = []
                continue
            path = stage_path(root, stage, "evidence.json")
            rows = read_json(path) if path.exists() else []
            if state["status"] == "complete":
                evidence[stage] = rows
            else:
                evidence[stage] = [
                    row for row in rows if row["id"] == f"harness/error/{stage}"
                ]
        if hashes == {stage: state_hash(root, stage) for stage in STAGE_ORDER}:
            return states, hashes, evidence


def _status(state):
    return state["status"] if state else "not started"


def _stage_rows(states, required):
    rows = []
    for stage in STAGE_ORDER:
        state = states[stage]
        note = ""
        if state:
            if stage == "quality" and state.get("gate"):
                note = f"gate {state['gate']}: {state.get('gate_reason', '')}"
            elif state.get("reason"):
                note = state["reason"]
        elif stage == "unit-tests":
            note = "optional"
        elif stage not in required:
            note = "not needed"
        rows.append(
            {
                "stage": stage,
                "status": _status(state),
                "required": stage in required,
                "host": (state or {}).get("machine", {}).get("host"),
                "seconds": (state or {}).get("seconds"),
                "attempts": (state or {}).get("attempts"),
                "note": note[:300],
                "reused": (state or {}).get("reused", {}),
            }
        )
    return rows


def _reuse_notes(states, store):
    notes = []
    for stage, field, label in (
        ("compile", "baseline_build", "baseline build"),
        ("quality", "baseline_quality", "baseline quality"),
        ("correctness", "baseline_correctness", "baseline correctness"),
        ("unit-tests", "baseline_unit_tests", "baseline unit tests"),
    ):
        value = ((states[stage] or {}).get("reused") or {}).get(field)
        if not value:
            continue
        if isinstance(value, list):
            notes.append(f"{label}: from the store ({len(value)} entries).")
            continue
        kind = {"baseline_correctness": "correctness", "baseline_unit_tests": "unit-tests"}.get(
            field
        )
        first = store.first_computed(kind, value) if store and kind else None
        when = f", first computed {first[:10]}" if first else ""
        notes.append(f"{label}: from the store (key {value[:12]}…{when}).")
    return notes


def _stored_baseline_note(root, run, policy, states, store):
    """With correctness not run, say whether the baseline's correctness is known."""
    if store is None or not run.get("builds", {}).get("baseline"):
        return None
    from qtb.coordinator import Comparison

    probe = Comparison.__new__(Comparison)
    probe.data, probe.run = data_root(), run
    try:
        identity = probe.verifier_identity()
    except (HarnessError, OSError):
        return None
    machine = (states["quality"] or {}).get("machine") or run.get("machine", {})
    key = baseline_correctness_key(run, policy, machine, identity, probe.data / "fixtures")
    entry = store.read_results("correctness", key) if key else None
    if entry is None:
        return "Baseline correctness: not known from the store for this baseline."
    preflight = next(
        (r for r in read_json(entry / "evidence.json") if r["id"] == "baseline/preflight"), None
    )
    result = preflight["result"] if preflight else "unknown"
    return (
        f"Baseline correctness: known from the store (key {key[:12]}…): preflight {result}."
        + (" The baseline is broken." if result == "failed" else "")
    )


def _cost_evidence_note(root, run):
    """Which kind of cost evidence the verdict rests on; unmonitored results stay labelled."""
    requirement = run.get("cost_evidence")
    if requirement:
        return (
            f"Cost evidence: monitored ({requirement.get('contract')}); every bundle is "
            f"checked by {requirement.get('validator')}."
        )
    modes = set()
    for path in sorted((root / "cost").glob("*/*.json")):
        try:
            modes.add(read_json(path).get("measurement_mode", "unlabelled"))
        except (HarnessError, OSError, AttributeError):
            continue
    if "unlabelled" in modes:
        return (
            "Cost evidence: unmonitored, from a harness that did not label its measurement "
            "mode; it is not monitored evidence."
        )
    if modes - {"machine"}:
        return (
            f"Cost evidence: {', '.join(sorted(modes))} mode, but this session requires no "
            "evidence contract, so no monitoring evidence was validated."
        )
    return "Cost evidence: machine mode (machine lock and load checks; no monitoring contract)."


def _progress_log(root, states):
    """Stage logs in graph order, then one table of every stage's step durations."""
    parts = []
    for stage in STAGE_ORDER:
        path = stage_path(root, stage, "progress.log")
        if path.exists():
            parts.append(f"##### {stage} #####\n" + path.read_text(errors="replace"))
    lines = ["", "Step durations (all stages)", "-" * 72]
    total = 0.0
    for stage in STAGE_ORDER:
        state = states[stage] or {}
        seconds = state.get("seconds")
        if seconds is None:
            lines.append(f"{stage:<50} {_status(states[stage]):>10}")
            continue
        total += seconds
        lines.append(f"{stage:<50} {duration(seconds):>10}")
        for entry in state.get("steps", []):
            shown = duration(entry["seconds"]) if entry.get("seconds") is not None else "—"
            label = ("  " * (entry.get("depth", 0) + 1) + entry["name"])[:50]
            flag = "" if entry.get("status") == "done" else f"  ({entry.get('status')})"
            lines.append(f"{label:<50} {shown:>10}{flag}")
    lines += ["-" * 72, f"{'Total (all stages)':<50} {duration(total):>10}", ""]
    atomic_bytes(root / "progress.log", ("\n".join(parts + lines)).encode())


def decide(results_root, progress=print):
    """Write the verdict for a session; returns ``(exit status, decision)``."""
    root = Path(results_root).resolve()
    if not (root / "run.json").exists():
        raise Precondition(f"{root} is not a session; run compile first")
    with locked(root / "lifecycle.lock", shared=True):
        with locked(root / "stages" / "decide.lock"):
            run = read_json(root / "run.json")
            states, hashes, by_stage = _snapshot(root)
            decision = _decide(root, run, states, hashes, by_stage)
            write_json(root / "evidence.json", decision.pop("_evidence"))
            write_report(root, decision)
            _progress_log(root, states)
    progress(f"{decision['status']} ({run['profile']}): {root}")
    return EXIT_CODES[decision["status"]], decision


def _decide(root, run, states, hashes, by_stage):
    manifest, policy = _archived_profile(root, run)
    prefix = profile_prefix(run["profile"])
    store = Store(run["store"]) if run.get("store") else None
    quality = states["quality"]
    gate_state = quality.get("gate") if quality and quality["status"] == "complete" else None
    required = ["compile", "quality"]
    if gate_state in {"improved", "aa"}:
        required.append("correctness")
        correctness = states["correctness"]
        if correctness and correctness["status"] == "complete":
            if cost_due(root, run, manifest, policy):
                required.append("cost")
    unit_tests = states["unit-tests"]
    if unit_tests and unit_tests["status"] in {"running", "failed", "complete"}:
        required.append("unit-tests")
    pending = [s for s in required if _status(states[s]) not in FINAL | {"failed"}]

    seen, evidence = {}, []
    for stage in STAGE_ORDER:
        for row in by_stage[stage]:
            if row["id"] in seen:
                raise HarnessError(
                    f"Record {row['id']} appears in both {seen[row['id']]} and {stage}"
                )
            seen[row["id"]] = stage
            evidence.append(row)
    notes = []
    preflight_failed = any(
        r["id"] == "baseline/preflight" and r["result"] == "failed" for r in evidence
    )
    # A running quality stage owns this append-only file. Its partial rows are not evidence.
    rows = read_records(root / "observations.jsonl") if _status(quality) == "complete" else []
    if preflight_failed:
        keep = {"compile", "correctness"}
        evidence = [r for r in evidence if seen[r["id"]] in keep or r["kind"] == "harness"]
        rows = []
        notes.append(
            "The baseline failed its correctness preflight: the quality evidence was set aside."
        )
    if _status(states["cost"]) == "complete":
        from qtb.coordinator.costs import replay_costs

        evidence = replay_costs(root, run, manifest, policy, evidence)
    if (
        not preflight_failed
        and _status(quality) == "complete"
        and _status(states["correctness"]) == "complete"
    ):
        clifford = read_records(root / "clifford.jsonl")
        evidence.append(stage_coverage(run, manifest["cases"], rows, clifford, prefix))
    records, required_ids, summaries = evaluate_quality(manifest, policy, rows, evidence)
    if "scope" in run:
        from types import SimpleNamespace

        from qtb.coordinator.costs import required_cost_panels

        panels = required_cost_panels(SimpleNamespace(run=run, manifest=manifest))
        required_ids = sorted(set(required_ids) | {f"{prefix}5/{name}" for name in panels})
    if "unit-tests" in required:
        required_ids = sorted(set(required_ids) | {f"{prefix}1/upstream"})

    if gate_state == "closed":
        notes.append(
            "Correctness, unit tests and cost not checked: the quality gate is closed "
            f"({quality.get('gate_reason')})."
        )
        note = _stored_baseline_note(root, run, policy, states, store)
        if note:
            notes.append(note)
    if _status(unit_tests) in {"not started", "skipped"}:
        notes.append("Upstream tests: not run.")
    for stage in STAGE_ORDER:
        if _status(states[stage]) == "noisy":
            notes.append(
                f"{stage}: no clean measurement. The last invocation detected interference "
                f"and was discarded ({states[stage].get('reason')}); this is not an observed "
                "regression."
            )
    if _status(states["cost"]) == "complete":
        notes.append(_cost_evidence_note(root, run))
    if pending:
        notes.append(
            "Unfinished required stages: "
            + ", ".join(f"{s} ({_status(states[s])})" for s in pending)
            + ". The verdict stays INCONCLUSIVE until they finish."
        )
    notes += _reuse_notes(states, store)
    if run["hashes"].get("implementation") not in {None, implementation_identity()}:
        notes.append("Decided by a different harness version than the one that compiled.")
    session = {
        "status": "INCONCLUSIVE" if pending else None,
        "stages": _stage_rows(states, required),
        "stage_state_hashes": hashes,
        "notes": notes,
    }
    decision = make_decision(
        run, records, required_ids, summaries, rows, manifest, policy, session=session
    )
    decision["_evidence"] = evidence
    return decision
