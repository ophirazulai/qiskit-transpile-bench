"""Separately runnable stages over one session directory.

::

    compile -> quality -> gate open? -- no -> correctness, unit-tests, cost record "skipped"
                            | yes
                   correctness   [unit-tests]     (unit-tests is optional)
                        |
                      cost                        (skipped if correctness found a failure)

Each stage owns ``stages/<stage>/``: ``state.json``, ``evidence.json`` and ``progress.log``,
plus its own output files. ``run.json`` is written only by ``compile``. ``decide`` merges the
evidence of committed stages; ``state.json: complete`` is the commit marker.

Every stage takes a shared session lifecycle lock and an exclusive stage lock, checks that the
session is not cleaned, loads ``run.json`` with the harness check of ``Comparison.open``,
checks its store entry, its prerequisites, the gate and the host, and only then writes
``running``. A finished stage (``complete`` or ``skipped``) never runs again in its session;
a ``running`` (killed) or ``failed`` stage resumes.

A stage's exit status reports whether it ran, not what it found: 0 for ``complete`` or
``skipped``, 40 when the stage failed, 41 when a precondition is not met, 64 for usage.
"""

import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from qtb.canonical import digest, file_hash, read_json, write_json
from qtb.coordinator import (
    Comparison,
    cost_stage_due,
    gate,
    quality_cases,
    same_build,
    worker_count,
)
from qtb.coordinator.runlog import step
from qtb.coordinator.storage import LockBusy, append_record, locked, read_records
from qtb.coordinator.store import resolve_store
from qtb.envbuild import SERIAL, host_mismatch
from qtb.errors import HarnessError, Incomplete, Precondition, Usage
from qtb.evaluator import evaluate_quality, record

STAGE_ORDER = ("compile", "quality", "correctness", "unit-tests", "cost")
PREREQUISITES = {
    "compile": (),
    "quality": ("compile",),
    "correctness": ("quality",),
    "unit-tests": ("quality",),
    "cost": ("correctness",),
}
GATED = {"correctness", "unit-tests", "cost"}
FINAL = {"complete", "skipped"}
STATE_FORMAT = "qtb-stage/1"
EXIT_OK, EXIT_ERROR, EXIT_PRECONDITION, EXIT_USAGE = 0, 40, 41, 64
# Files at the top of a directory that compile is still creating.
SESSION_SEED = {"lifecycle.lock", "stages"}


# State files


def stage_path(root, stage, name):
    return Path(root) / "stages" / stage / name


def read_state(root, stage):
    path = stage_path(root, stage, "state.json")
    return read_json(path) if path.exists() else None


def state_hash(root, stage):
    path = stage_path(root, stage, "state.json")
    return file_hash(path) if path.exists() else None


def committed_evidence(root, stage):
    """``complete``: the stage's evidence; ``failed``: only its crash record; else nothing."""
    state = read_state(root, stage)
    if state is None or state["status"] not in {"complete", "failed"}:
        return []
    path = stage_path(root, stage, "evidence.json")
    rows = read_json(path) if path.exists() else []
    if state["status"] == "failed":
        return [r for r in rows if r["id"] == f"harness/error/{stage}"]
    return rows


def clean_status(root):
    path = Path(root) / "clean.json"
    return read_json(path).get("status") if path.exists() else None


def scheduler_info():
    names = ("LSB_JOBID", "LSB_QUEUE", "LSB_HOSTS", "LSB_MCPU_HOSTS", "LSB_DJOB_NUMPROC")
    return {name: os.environ[name] for name in names if name in os.environ}


@contextmanager
def session_locks(root, stage):
    """The shared lifecycle lock (``clean`` takes it exclusively), then the stage lock."""
    root = Path(root)
    try:
        with locked(root / "lifecycle.lock", shared=True, wait=False):
            try:
                with locked(stage_path(root, stage, "lock"), wait=False):
                    yield
            except LockBusy as exc:
                raise Precondition(f"{stage} is already running for {root}") from exc
    except LockBusy as exc:
        raise Precondition(f"{root} is being cleaned") from exc


def refuse_cleaned(root):
    status = clean_status(root)
    if status == "cleaning":
        raise Precondition("Session cleanup was interrupted; run clean again to finish it")
    if status:
        raise Precondition("Session cleaned: start a new session to measure again")


# Stage bodies. Each returns fields for its final state.


def compile_body(comparison):
    cases = quality_cases(comparison.manifest)
    comparison.progress(
        f"{comparison.run['profile']}: {len(cases)} quality cases, "
        f"{sum(c['seeds_per_block'] for c in cases)} compiles per revision before checks."
    )
    with step(comparison, "Build"):
        comparison.build()
    with step(comparison, "Input roundtrip"):
        comparison.roundtrip(comparison.roundtrip_cases())
    baseline = comparison.run["builds"]["baseline"]
    return {"reused": {"baseline_build": baseline["store_key"]} if baseline.get("reused") else {}}


def quality_body(comparison):
    cases = quality_cases(comparison.manifest)
    with step(comparison, "Quality (C0 + C6 routing replay)"):
        rows = comparison.quality(cases)
    with step(comparison, "Determinism audit"):
        comparison.audit(cases, rows)
    with step(comparison, "Aggregate checks"):
        comparison.aggregate_checks(cases, rows)
    records, _, _ = evaluate_quality(
        comparison.manifest,
        comparison.policy,
        rows,
        comparison.committed("compile") + comparison.records,
    )
    state, reason = gate(comparison.run["builds"], records)
    comparison.progress(f"Gate: {state}: {reason}.")
    reused = sorted({r["cached_from"] for r in rows if r.get("cached_from")})
    return {
        "gate": state,
        "gate_reason": reason,
        "reused": {"baseline_quality": reused} if reused else {},
    }


def baseline_correctness_key(run, policy, machine, verifier_identity, fixtures):
    """Store key of the baseline correctness half, or ``None`` when it must be recomputed."""
    from qtb.coordinator.checks import CLIFFORD_FULL_MODE, CLIFFORD_SEEDS

    if not run.get("store") or same_build(run.get("builds", {})):
        return None
    prefix = "CA" if run["profile"] == "confirm-profile" else "IA"
    return digest(
        {
            "build": run["builds"]["baseline"]["id"],
            "implementation": run["hashes"].get("implementation"),
            "harness": run["hashes"].get("harness"),
            "suite": file_hash(Path(fixtures) / "correctness-suite.json"),
            "manifest": run["hashes"]["manifest"],
            "policy": run["hashes"]["policy"],
            "verifier": verifier_identity,
            "machine_cpu": machine.get("cpu"),
            "worker_environment": SERIAL,
            "clifford": [CLIFFORD_SEEDS[prefix], CLIFFORD_FULL_MODE[prefix]],
            "tolerances": policy.get("tolerances"),
        }
    )


def is_baseline_correctness(id_, prefix):
    return id_.startswith(("behavior/baseline/", "api/baseline/", "C7/baseline/")) or id_ in {
        f"{prefix}1/C1-C5/baseline",
        "baseline/preflight",
    }


def correctness_body(comparison):
    from qtb.coordinator.checks import behavior_checks, clifford_checks

    # A retry restarts the suites; the verifier cache makes repeated checks cheap.
    for name in ("correctness.jsonl", "clifford.jsonl"):
        (comparison.directory / name).unlink(missing_ok=True)
    comparison.reset_evidence()
    cases = quality_cases(comparison.manifest)
    key = baseline_correctness_key(
        comparison.run,
        comparison.policy,
        comparison.machine,
        comparison.verifier_identity(),
        comparison.fixtures,
    )
    entry = comparison.store.read_results("correctness", key) if key else None
    reused = {}
    if entry is not None:
        comparison.progress(f"Baseline correctness: from the store (key {key[:12]}).")
        for line in read_records(entry / "rows.jsonl"):
            append_record(comparison.directory / line["file"], dict(line["row"], cached_from=key))
        for row in read_json(entry / "evidence.json"):
            comparison.evidence(dict(row, cached_from=key))
        reused["baseline_correctness"] = key
    else:
        with step(comparison, "C1-C5 suite (baseline)"):
            decisive = behavior_checks(comparison, revisions=("baseline",))
        with step(comparison, "C7 Clifford variants (baseline)"):
            decisive &= clifford_checks(comparison, cases, revisions=("baseline",))
        baseline_bad = any(
            r["subject"] == "reference" and r["result"] == "failed" for r in comparison.records
        )
        comparison.evidence(
            record(
                "baseline/preflight",
                "correctness",
                "failed" if baseline_bad else "passed",
                "reference",
            )
        )
        if key and decisive:
            store_baseline_correctness(comparison, key)
    preflight = next(r for r in comparison.records if r["id"] == "baseline/preflight")
    if preflight["result"] == "failed":
        comparison.progress("The baseline failed its correctness preflight; evolved not checked.")
        return {"reused": reused}
    with step(comparison, "C1-C5 suite (evolved)"):
        behavior_checks(comparison, revisions=("evolved",))
    with step(comparison, "C7 Clifford variants (evolved)"):
        clifford_checks(comparison, cases, revisions=("evolved",))
    return {"reused": reused}


def store_baseline_correctness(comparison, key):
    """Publish the baseline rows and records; only called when every check was decisive."""
    rows = [
        {"file": name, "row": row}
        for name in ("correctness.jsonl", "clifford.jsonl")
        for row in read_records(comparison.directory / name)
        if row.get("revision") == "baseline"
    ]
    records = [r for r in comparison.records if is_baseline_correctness(r["id"], comparison.prefix)]

    def fill(partial, _final):
        for row in rows:
            append_record(partial / "rows.jsonl", row)
        write_json(partial / "evidence.json", records)

    comparison.store.publish_results(
        "correctness", key, fill, {"session": str(comparison.directory)}
    )


def unit_tests_body(comparison):
    from qtb.coordinator.upstream import baseline_unit_tests_key, upstream_checks

    comparison.reset_evidence()
    with step(comparison, "Upstream Qiskit tests"):
        upstream_checks(comparison)
    key = baseline_unit_tests_key(comparison)
    cached = any(r.get("cached_from") for r in comparison.records)
    return {"reused": {"baseline_unit_tests": key} if cached else {}}


def cost_due(root, run, manifest, policy):
    """``cost_stage_due`` over the committed compile, quality and correctness evidence."""
    rows = read_records(Path(root) / "observations.jsonl")
    evidence = [
        row
        for stage in ("compile", "quality", "correctness")
        for row in committed_evidence(root, stage)
    ]
    records, _, _ = evaluate_quality(manifest, policy, rows, evidence)
    return cost_stage_due(run["builds"], records)


def skip_reason(root, stage, run=None, manifest=None, policy=None):
    """Why a gated stage records ``skipped`` instead of running, or ``None``.

    Callable without a ``Comparison`` (``tools/lsf/cost_if_gated.sh`` uses it), in which
    case the session's archived profile is read.
    """
    quality = read_state(root, "quality")
    if quality is None or quality["status"] != "complete":
        return None
    if quality.get("gate") == "closed":
        return f"gate closed: {quality.get('gate_reason')}"
    if stage != "cost":
        return None
    correctness = read_state(root, "correctness")
    if correctness is None or correctness["status"] != "complete":
        return None
    if run is None:
        run = read_json(Path(root) / "run.json")
        manifest = read_json(Path(root) / "manifest.json")
        policy = read_json(Path(root) / "policy.json")
    if not cost_due(root, run, manifest, policy):
        return "correctness found a failure"
    return None


def cost_body(comparison):
    from qtb.coordinator.costs import measure_costs

    due = cost_due(comparison.directory, comparison.run, comparison.manifest, comparison.policy)
    if due == "aa":
        comparison.progress("Identical builds: measuring cost panels as an A/A check.")
    with step(comparison, "Cost panels (timing/memory)"):
        try:
            measure_costs(comparison)
        except Incomplete as exc:
            comparison.evidence(
                record(f"{comparison.prefix}5/timing", "cost", "unresolved", detail=str(exc))
            )
    return {"cost_due": due}


BODIES = {
    "compile": compile_body,
    "quality": quality_body,
    "correctness": correctness_body,
    "unit-tests": unit_tests_body,
    "cost": cost_body,
}


# Running a stage


def _inputs(comparison):
    hashes = comparison.run["hashes"]
    return {
        "coordinator": hashes.get("coordinator"),
        "implementation": hashes.get("implementation"),
        "harness": hashes.get("harness"),
        "manifest": hashes["manifest"],
        "policy": hashes["policy"],
        "builds": {k: v["id"] for k, v in comparison.run.get("builds", {}).items()},
    }


def _write_state(root, stage, state):
    write_json(stage_path(root, stage, "state.json"), dict(state, format=STATE_FORMAT, stage=stage))


def _skip(comparison, previous, reason):
    comparison.progress(f"{comparison.stage}: skipped: {reason}.")
    now = datetime.now(UTC).isoformat()
    _write_state(
        comparison.directory,
        comparison.stage,
        {
            "status": "skipped",
            "started_at": now,
            "finished_at": now,
            "seconds": 0,
            "attempts": (previous or {}).get("attempts", 0),
            "machine": comparison.machine,
            "scheduler": scheduler_info(),
            "inputs": _inputs(comparison),
            "reason": reason,
        },
    )
    return EXIT_OK


def _preconditions(comparison, previous):
    """Store, prerequisites, gate and host; returns an exit code when the stage ends here."""
    root, stage = comparison.directory, comparison.stage
    for needed in PREREQUISITES[stage]:
        state = read_state(root, needed)
        status = state["status"] if state else "not started"
        if status not in FINAL:
            raise Precondition(
                f"{stage} needs {needed} to be complete (it is {status}); run: "
                f"qiskit-transpile-bench {needed} --results-root {root}"
            )
    if stage != "compile":
        store = comparison.store
        if store is None:
            raise Precondition("The session has no store; it was not created by compile")
        store.require()
        key = comparison.run.get("builds", {}).get("baseline", {}).get("store_key")
        if key is None or store.ready_build(key) is None:
            raise Precondition(
                f"Baseline build {key} is missing from the store {store.root}; "
                "start a new session"
            )
    if stage in GATED:
        reason = skip_reason(root, stage, comparison.run, comparison.manifest, comparison.policy)
        if reason:
            return _skip(comparison, previous, reason)
    if stage != "compile":
        for revision, build in comparison.run["builds"].items():
            problem = host_mismatch(build)
            if problem:
                raise Precondition(f"This host cannot run the {revision} build: {problem}")
    return None


def _execute(comparison, previous, progress):
    root, stage = comparison.directory, comparison.stage
    ended = _preconditions(comparison, previous)
    if ended is not None:
        return ended
    started, clock = datetime.now(UTC).isoformat(), time.monotonic()
    state = {
        "status": "running",
        "started_at": started,
        "attempts": (previous or {}).get("attempts", 0) + 1,
        "machine": comparison.machine,
        "scheduler": scheduler_info(),
        "inputs": _inputs(comparison),
        "workers": worker_count(),
    }
    _write_state(root, stage, state)
    # The retried attempt's crash record is gone from memory; commit that now.
    write_json(comparison.evidence_path, comparison.records)
    try:
        fields = BODIES[stage](comparison)
        status, code = "complete", EXIT_OK
    except Exception as exc:  # any crash fails the stage; a kill leaves it running
        detail = str(exc) if isinstance(exc, HarnessError) else f"{type(exc).__name__}: {exc}"
        comparison.evidence(record(f"harness/error/{stage}", "harness", "failed", detail=detail))
        fields, status, code = {"reason": detail}, "failed", EXIT_ERROR
        progress(f"ERROR in {stage}: {detail}")
    summary = getattr(comparison.progress, "summary", None)
    if summary:
        summary()
    write_json(comparison.evidence_path, comparison.records)
    state.update(
        fields,
        status=status,
        finished_at=datetime.now(UTC).isoformat(),
        seconds=time.monotonic() - clock,
        inputs=_inputs(comparison),
        steps=[dict(s) for s in getattr(comparison.progress, "steps", [])],
    )
    _write_state(root, stage, state)
    progress(f"{stage}: {status} ({root})")
    return code


def run_stage(results_root, stage, progress=print):
    """Run one stage after ``compile``; returns the stage's exit status."""
    if stage not in STAGE_ORDER or stage == "compile":
        raise Usage(f"Unknown stage {stage}")
    root = Path(results_root).resolve()
    if not (root / "run.json").exists():
        raise Precondition(f"{root} is not a session; run compile first")
    with session_locks(root, stage):
        refuse_cleaned(root)
        previous = read_state(root, stage)
        if previous and previous["status"] in FINAL:
            progress(f"{stage}: already {previous['status']} ({root})")
            return EXIT_OK
        comparison = Comparison.open(root, stage, progress)
        return _execute(comparison, previous, progress)


def _check_session_directory(root):
    if root.exists() and not root.is_dir():
        raise Usage(f"{root} exists and is not a directory")
    if root.is_dir() and not (root / "run.json").exists():
        extra = sorted(p.name for p in root.iterdir() if p.name not in SESSION_SEED)
        if extra:
            raise Usage(
                f"{root} exists and is not a session (it contains {', '.join(extra[:3])}); "
                "choose a new --results-root"
            )


def run_compile(baseline, evolved, profile, results_root, store=None, progress=print):
    """Create the session, or resume its unfinished compile; returns the exit status."""
    store = resolve_store(store)
    root = Path(results_root).resolve()
    _check_session_directory(root)
    root.mkdir(parents=True, exist_ok=True)
    with session_locks(root, "compile"):
        _check_session_directory(root)
        refuse_cleaned(root)
        previous = read_state(root, "compile")
        if (root / "run.json").exists():
            # Other inputs are a usage error even when compile has finished.
            run = read_json(root / "run.json")
            wanted = {
                "sources": {
                    "baseline": str(Path(baseline).resolve()),
                    "evolved": str(Path(evolved).resolve()),
                },
                "store": str(store),
                "profile": profile,
            }
            differ = [k for k, v in wanted.items() if run.get(k) != v]
            if differ:
                raise Usage(
                    f"{root} is a session for other inputs ({', '.join(differ)} differ); "
                    "start a new session with another --results-root"
                )
        if previous and previous["status"] in FINAL:
            progress(f"compile: already {previous['status']} ({root})")
            return EXIT_OK
        comparison = Comparison.create(baseline, evolved, profile, root, store, progress)
        return _execute(comparison, previous, progress)


def session_summary(root):
    """Every stage's state, for the report and for ``clean``'s consistency check."""
    return {stage: read_state(root, stage) for stage in STAGE_ORDER}

