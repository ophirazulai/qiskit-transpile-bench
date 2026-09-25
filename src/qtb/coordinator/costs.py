"""Exclusive, interleaved fresh two-arm cost sessions judged by policy thresholds."""

import os
import random
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from qtb.canonical import digest, read_json, write_json
from qtb.config import STAGES
from qtb.coordinator.process import run_worker
from qtb.coordinator.runlog import step
from qtb.coordinator.storage import locked, runner_lock
from qtb.envbuild import machine_identity
from qtb.errors import HarnessError, Incomplete
from qtb.evaluator import record
from qtb.evaluator.cost import (
    MEASURED_ARMS,
    cost_guard,
    regime_counts,
    thresholds_id,
    validate_bundle,
)

REGIMES = ("screen", "normal", "rerun")


def unknown_scope(comparison):
    """A change that maps to every stage, or to unmapped paths, could affect anything."""
    return any(
        set(value["stages"]) == set(STAGES) or value.get("unmapped_paths")
        for value in comparison.run["scope"].values()
    )


def cost_panels(comparison):
    """Cost panels to measure for this comparison's change scope.

    ``timing``, ``confirm-timing``, ``memory`` and ``preset`` are always measured.
    ``timing-basis`` (the cx/ecr twins of the scored circuits) is measured only
    when the change can reach translation or optimization, since it repeats the
    cz cases' layout and routing. The multi-seed ``companion`` is measured only
    when the change can reach layout or routing.
    """
    cases = comparison.manifest["cases"]
    panels = {
        name: (
            [c for c in cases if c.get("panel") == name],
            "memory" if name == "memory" else "timing",
        )
        for name in ("timing", "timing-basis", "preset", "confirm-timing", "memory")
    }
    panels = {name: pair for name, pair in panels.items() if pair[0]}
    scope = {s for value in comparison.run["scope"].values() for s in value["stages"]}
    unknown = unknown_scope(comparison)
    if not unknown and not scope & {"translation", "optimization"}:
        panels.pop("timing-basis", None)
    if unknown or scope & {"layout", "routing"}:
        if comparison.run["profile"] == "confirm-profile":
            groups = {
                c["input_group"]
                for c in cases
                if c.get("panel") == "memory" and c["input_group"] != "multiplier_h18_n20"
            }
            companion = [
                next(
                    c
                    for c in cases
                    if c["input_group"] == group
                    and c["role"] == "scored"
                    and c["optimization_level"] == 2
                )
                for group in sorted(groups)
            ]
        else:
            companion = [
                c for c in cases if c.get("panel") == "timing" and c["modes"] == ["timing_reuse"]
            ]
        panels["companion"] = (companion, "companion")
    return panels


def required_cost_panels(comparison):
    """The preset panel is measured always, but guarded for relevant or unknown scope."""
    panels = cost_panels(comparison)
    preset_scope = unknown_scope(comparison)
    changed_paths = (
        path.lower().replace("\\", "/") for path in comparison.run.get("changed_paths", [])
    )
    preset_paths = any(
        "preset_passmanager" in path or "/target" in path for path in changed_paths
    )
    if not preset_scope and not preset_paths:
        panels.pop("preset", None)
    return panels


def interleaving_seed(run_id, cases, estimator, count, arms):
    """Bind cost arm order to this comparison and this panel's measurement plan."""
    return int(
        digest(
            {
                "run_id": run_id,
                "cases": [case["case_id"] for case in cases],
                "estimator": estimator,
                "count": count,
                "arms": list(arms),
            }
        )[:16],
        16,
    )


def _too_busy():
    return os.getloadavg()[0] > max(1.0, (os.cpu_count() or 1) * 0.5)


def _wait_until_quiet(timeout_s=300, poll_s=10):
    """Let the one-minute load average decay before measuring; a second line of defence.

    On a cluster the cost stage should already have an exclusive host (``bsub -x``).
    """
    deadline = time.monotonic() + timeout_s
    while _too_busy():
        if time.monotonic() >= deadline:
            raise Incomplete("Machine is too busy for cost measurement")
        time.sleep(poll_s)


def timing_batch_job(cases, fixture_root, measurement_protocol):
    """One fresh process times every case of a panel, in manifest order.

    Python start-up, the Qiskit import and fixture loading are paid once per
    (round, arm) instead of once per case. Each batch entry is still loaded and
    warmed up on its own, and the worker writes one heartbeat row per entry, so
    the per-entry timeout and the retry-from-the-next-entry rule still apply.
    The seed list indexes the batch; the case's own fixed seed lives in the entry.
    """
    return {
        "mode": "timing_batch",
        "batch": [
            {
                "case": case,
                "mode": case["modes"][0],
                "seed": case.get("timing", {}).get("fixed_seed", 0),
            }
            for case in cases
        ],
        "seeds": list(range(len(cases))),
        "fixture_root": str(fixture_root),
        "timeout_s": max(120, max(case["timeout_s"] for case in cases) * 5),
        "warmups": measurement_protocol["warmups"],
        "minimum_calls": measurement_protocol["minimum_calls"],
        "minimum_ns": measurement_protocol["minimum_ns"],
    }


def collect_panel(
    run, builds, cases, estimator, directory, count, arms, fixture_root, measurement_protocol
):
    session = uuid.uuid4().hex
    rng_seed = interleaving_seed(run["run_id"], cases, estimator, count, arms)
    rng = random.Random(rng_seed)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    session_directory = directory / session
    session_directory.mkdir()
    samples = {
        arm: {case["case_id"]: {} if estimator == "companion" else [] for case in cases}
        for arm in arms
    }
    arm_ids = {arm: uuid.uuid4().hex for arm in arms}

    def collect(arm, job, job_id, expected):
        if _too_busy():
            raise Incomplete("Machine became busy during cost measurement")
        results = run_worker(builds[arm], job, session_directory / job_id)
        by_seed = {result["seed"]: result for result in results}
        if len(results) != len(expected) or set(by_seed) != set(expected):
            raise Incomplete(f"Incomplete cost worker batch: {job_id}")
        for seed in expected:
            if by_seed[seed]["status"] != "ok":
                raise Incomplete(f"Cost worker failed: {by_seed[seed].get('error')}")
        return by_seed

    with locked(runner_lock()):
        _wait_until_quiet()
        for round_ in range(count):
            if estimator == "timing":
                # A timing round is one fresh process per arm running the whole
                # panel. Arms are interleaved per round in random order.
                order = list(arms)
                rng.shuffle(order)
                job = timing_batch_job(cases, fixture_root, measurement_protocol)
                for arm in order:
                    by_index = collect(arm, job, f"{round_}-batch-{arm}", job["seeds"])
                    for index, case in enumerate(cases):
                        samples[arm][case["case_id"]].append(by_index[index]["samples_ns"])
                continue
            for case in cases:
                seeds = (
                    list(range(20))
                    if estimator == "companion"
                    else [case.get("timing", {}).get("fixed_seed", 0)]
                )
                # A companion round is one fresh process per arm and case. The
                # worker builds a pass manager outside the clock for each seed,
                # then emits a heartbeat after each measured seed. Memory is one
                # fresh process per arm and case, so peak RSS is that compile's.
                order = list(arms)
                rng.shuffle(order)
                for arm in order:
                    job_id = f"{round_}-{digest(case['case_id'])[:12]}-{arm}"
                    mode = "memory" if estimator == "memory" else "timing_reuse"
                    job = {
                        "mode": mode,
                        "case": case,
                        "seeds": seeds,
                        "fixture_root": str(fixture_root),
                        "timeout_s": max(120, case["timeout_s"] * 5),
                    }
                    if mode != "memory":
                        job.update(
                            warmups=measurement_protocol["warmups"],
                            minimum_calls=measurement_protocol["minimum_calls"],
                            minimum_ns=measurement_protocol["minimum_ns"],
                        )
                    by_seed = collect(arm, job, job_id, seeds)
                    for seed in seeds:
                        result = by_seed[seed]
                        value = (
                            result["peak_rss_bytes"]
                            if estimator == "memory"
                            else result["samples_ns"]
                        )
                        entry = samples[arm][case["case_id"]]
                        if estimator == "companion":
                            entry.setdefault(str(seed), []).append(value)
                        else:
                            entry.append(value)
    return {
        "session_id": session,
        "arms": {
            arm: {
                "arm_id": arm_ids[arm],
                "session_id": session,
                "build_id": builds[arm]["id"],
                "samples": samples[arm],
            }
            for arm in arms
        },
        "run_id": run["run_id"],
        # The cost host, which need not be the host that compiled the session.
        "machine": machine_identity(),
        "measured_at": datetime.now(UTC).isoformat(),
        "complete": True,
        "estimator": estimator,
        "interleaving_seed": rng_seed,
        "case_hashes": {c["case_id"]: digest(c) for c in cases},
        "timing_protocol": {
            name: measurement_protocol[name]
            for name in ("warmups", "minimum_calls", "minimum_ns")
        },
    }


def panel_weights(cases, estimator):
    if estimator != "memory":
        return {c["case_id"]: 1 / len(cases) for c in cases}
    families = {c["family"] for c in cases}
    return {
        c["case_id"]: 1 / len(families) / sum(o["family"] == c["family"] for o in cases)
        for c in cases
    }


def measure_panel(run, builds, cases, estimator, directory, fixture_root, policy, guarded=True):
    """Screen, then measure in full, then rerun once; each regime a fresh session.

    A clear screen ends the panel early. A report-only panel never pays for a
    rerun, but does complete the full measurement when its screen is unclear.
    """
    protocol = policy["measurement_protocol"]
    weights = panel_weights(cases, estimator)
    counts = regime_counts(estimator, protocol)
    results = []
    for regime in (r for r in REGIMES if r in counts):
        if regime == "rerun" and not guarded:
            break
        saved = Path(directory) / f"{regime}.json"
        bundle = read_json(saved) if saved.exists() else None
        if bundle is not None:
            try:
                validate_bundle(bundle, policy, estimator, weights, run["run_id"])
            except Incomplete:
                bundle = None
        if bundle is None:
            bundle = collect_panel(
                run,
                builds,
                cases,
                estimator,
                Path(directory) / regime,
                counts[regime],
                list(MEASURED_ARMS),
                fixture_root,
                protocol,
            )
            bundle.update(regime=regime, thresholds_id=thresholds_id(policy))
        write_json(saved, bundle)
        result = cost_guard(bundle, policy, estimator, weights, run["run_id"])
        results.append(result)
        if regime == "screen" and result.get("needs_full"):
            continue
        if regime == "normal" and result.get("needs_rerun"):
            continue
        break
    return results[-1]


def measure_costs(comparison):
    guarded = required_cost_panels(comparison)
    for name, (cases, estimator) in cost_panels(comparison).items():
        comparison.progress(f"Measuring {name}: fresh interleaved baseline/evolved arms.")
        try:
            with step(comparison, f"cost panel {name}"):
                result = measure_panel(
                    comparison.run,
                    comparison.run["builds"],
                    cases,
                    estimator,
                    comparison.directory / "cost" / name,
                    comparison.fixtures,
                    comparison.policy,
                    guarded=name in guarded,
                )
        except Incomplete as exc:
            if name in guarded:
                raise
            comparison.evidence(
                record(
                    f"{comparison.prefix}5/{name}",
                    "cost",
                    "not_evaluated",
                    observed_result="unresolved",
                    detail=str(exc),
                )
            )
            continue
        details = {k: v for k, v in result.items() if k != "result"}
        if name not in guarded:
            details["observed_result"] = result["result"]
        comparison.evidence(
            record(
                f"{comparison.prefix}5/{name}",
                "cost",
                result["result"] if name in guarded else "not_evaluated",
                **details,
            )
        )


def replay_costs(directory, run, manifest, policy, evidence):
    """Recompute complete cost decisions from raw bundles, never cached verdicts."""
    from types import SimpleNamespace

    if "scope" not in run:
        return evidence
    prefix = "CA" if run["profile"] == "confirm-profile" else "IA"
    evidence = list(evidence)
    comparison = SimpleNamespace(run=run, manifest=manifest)
    guarded = required_cost_panels(comparison)
    for name, (cases, estimator) in cost_panels(comparison).items():
        id_ = f"{prefix}5/{name}"
        path = Path(directory) / "cost" / name
        bundles = {r: path / f"{r}.json" for r in REGIMES}
        if not any(b.exists() for b in bundles.values()) and not any(
            r["id"] == id_ for r in evidence
        ):
            continue
        if "cost_thresholds" not in policy:
            raise HarnessError(
                "Archived policy has no cost_thresholds (a pre-version-4 run); "
                "re-evaluate it with the harness wheel archived in that run"
            )
        weights = panel_weights(cases, estimator)
        counts = regime_counts(estimator, policy["measurement_protocol"])
        try:

            def check(
                regime,
                previous=None,
                cases=cases,
                bundles=bundles,
                weights=weights,
                estimator=estimator,
            ):
                if not bundles[regime].exists():
                    raise Incomplete(f"Missing {regime} cost bundle")
                bundle = read_json(bundles[regime])
                if bundle["regime"] != regime:
                    raise Incomplete(f"Expected the {regime} bundle")
                if previous is not None and bundle["session_id"] == previous["session_id"]:
                    raise Incomplete(f"The {regime} bundle must be a fresh session")
                if bundle["case_hashes"] != {c["case_id"]: digest(c) for c in cases}:
                    raise Incomplete("Cost bundle case definitions changed")
                if any(
                    bundle["arms"][a]["build_id"] != run["builds"][a]["id"]
                    for a in MEASURED_ARMS
                ):
                    raise Incomplete("Cost bundle build identities changed")
                return bundle, cost_guard(bundle, policy, estimator, weights, run["run_id"])

            previous = None
            if "screen" in counts:
                previous, result = check("screen")
                if result["needs_full"]:
                    previous, result = check("normal", previous)
            else:
                previous, result = check("normal")
            if name in guarded and result["needs_rerun"]:
                if not bundles["rerun"].exists():
                    raise Incomplete("Cost breach requires a fresh doubled-count rerun")
                _, result = check("rerun", previous)
            details = {k: v for k, v in result.items() if k != "result"}
            if name not in guarded:
                details["observed_result"] = result["result"]
            result = record(
                id_, "cost", result["result"] if name in guarded else "not_evaluated", **details
            )
        except (Incomplete, FileNotFoundError) as exc:
            result = record(
                id_,
                "cost",
                "unresolved" if name in guarded else "not_evaluated",
                detail=str(exc),
            )
        evidence = [r for r in evidence if r["id"] != id_] + [result]
    return evidence
