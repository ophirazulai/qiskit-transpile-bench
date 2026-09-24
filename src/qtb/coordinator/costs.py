"""Exclusive, interleaved fresh cost sessions and estimator-matched calibration."""

import os
import random
import uuid
from datetime import UTC, datetime
from pathlib import Path

from qtb.canonical import digest, read_json, write_json
from qtb.coordinator.process import run_worker
from qtb.coordinator.storage import locked, runner_lock
from qtb.errors import Incomplete
from qtb.evaluator.cost import calibrate_cost, cost_guard, regime_counts

REGIMES = ("screen", "normal", "rerun")


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
        if _too_busy():
            raise Incomplete("Machine is too busy for cost measurement")
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
        "machine": run["machine"],
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


def calibrate_panel(run, builds, cases, estimator, directory, fixture_root, measurement_protocol):
    _, collected = regime_counts(estimator, measurement_protocol)
    bundle = collect_panel(
        run,
        builds,
        cases,
        estimator,
        directory,
        collected,
        ["baseline", "control"],
        fixture_root,
        measurement_protocol,
    )
    raw = {arm: data["samples"] for arm, data in bundle["arms"].items()}
    calibration = calibrate_cost(
        raw,
        estimator,
        panel_weights(cases, estimator),
        run["machine"],
        bundle["measured_at"],
        protocol=measurement_protocol,
    )
    write_json(Path(directory) / "raw.json", bundle)
    write_json(Path(directory) / "calibration.json", calibration)
    return calibration


def measure_panel(
    run, builds, cases, estimator, directory, fixture_root, calibration,
    measurement_protocol, guarded=True,
):
    """Screen, then measure in full, then rerun once; each regime a fresh session.

    A clear screen ends the panel early. A report-only panel never pays for a
    rerun, but does complete the full measurement when its screen is unclear.
    """
    results = []
    for regime in (r for r in REGIMES if r in calibration["regimes"]):
        if regime == "rerun" and not guarded:
            break
        count = calibration["regimes"][regime]["count"]
        saved = Path(directory) / f"{regime}.json"
        bundle = read_json(saved) if saved.exists() else None
        if bundle is not None:
            try:
                from qtb.evaluator.cost import validate_bundle

                validate_bundle(bundle, calibration, run["run_id"], historical=False)
            except Incomplete:
                bundle = None
        if bundle is None:
            bundle = collect_panel(
                run,
                builds,
                cases,
                estimator,
                Path(directory) / regime,
                count,
                ["baseline", "control", "evolved"],
                fixture_root,
                measurement_protocol,
            )
            bundle.update(regime=regime, calibration_id=calibration["id"])
        write_json(saved, bundle)
        result = cost_guard(bundle, calibration, run["run_id"])
        results.append(result)
        if regime == "screen" and result.get("needs_full"):
            continue
        if regime == "normal" and result.get("needs_rerun"):
            continue
        break
    return results[-1]


def replay_costs(directory, run, manifest, evidence):
    """Recompute complete cost decisions from raw bundles, never cached verdicts."""
    from types import SimpleNamespace

    from qtb.coordinator.calibration import cost_panels, required_cost_panels
    from qtb.evaluator import record

    if not run.get("calibrations") or "scope" not in run:
        return evidence
    prefix = "CA" if run["profile"] == "confirm-profile" else "IA"
    evidence = list(evidence)
    comparison = SimpleNamespace(run=run, manifest=manifest)
    guarded = required_cost_panels(comparison)
    for name, (cases, _estimator) in cost_panels(comparison).items():
        id_ = f"{prefix}5/{name}"
        path = Path(directory) / "cost" / name
        bundles = {r: path / f"{r}.json" for r in REGIMES}
        if not any(b.exists() for b in bundles.values()) and not any(
            r["id"] == id_ for r in evidence
        ):
            continue
        calibration = run["calibrations"]["cost"][name]
        try:

            def check(
                regime, previous=None, cases=cases, calibration=calibration, bundles=bundles
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
                    for a in ("baseline", "control", "evolved")
                ):
                    raise Incomplete("Cost bundle build identities changed")
                return bundle, cost_guard(bundle, calibration, run["run_id"], historical=True)

            previous = None
            if "screen" in calibration.get("regimes", {}):
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
