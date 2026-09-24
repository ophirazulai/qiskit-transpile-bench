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
from qtb.evaluator.cost import COUNTS, calibrate_cost, cost_guard


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
    with locked(runner_lock()):
        if os.getloadavg()[0] > max(1.0, (os.cpu_count() or 1) * 0.5):
            raise Incomplete("Machine is too busy for cost measurement")
        for round_ in range(count):
            for case in cases:
                seeds = (
                    list(range(20))
                    if estimator == "companion"
                    else [case.get("timing", {}).get("fixed_seed", 0)]
                )
                # A companion round is one fresh process per arm and case. The
                # worker builds a pass manager outside the clock for each seed,
                # then emits a heartbeat after each measured seed.
                order = list(arms)
                rng.shuffle(order)
                for arm in order:
                    if os.getloadavg()[0] > max(1.0, (os.cpu_count() or 1) * 0.5):
                        raise Incomplete("Machine became busy during cost measurement")
                    job_id = f"{round_}-{digest(case['case_id'])[:12]}-{arm}"
                    mode = (
                        "memory"
                        if estimator == "memory"
                        else "timing_reuse"
                        if estimator == "companion"
                        else case["modes"][0]
                    )
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
                    results = run_worker(builds[arm], job, session_directory / job_id)
                    by_seed = {result["seed"]: result for result in results}
                    if len(results) != len(seeds) or set(by_seed) != set(seeds):
                        raise Incomplete(f"Incomplete cost worker batch: {case['case_id']}")
                    for seed in seeds:
                        result = by_seed[seed]
                        if result["status"] != "ok":
                            raise Incomplete(f"Cost worker failed: {result.get('error')}")
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
    collected = collect_panel(
        run,
        builds,
        cases,
        estimator,
        directory,
        COUNTS[estimator][2],
        ["baseline", "control"],
        fixture_root,
        measurement_protocol,
    )
    raw = {arm: data["samples"] for arm, data in collected["arms"].items()}
    calibration = calibrate_cost(
        raw, estimator, panel_weights(cases, estimator), run["machine"], collected["measured_at"]
    )
    write_json(Path(directory) / "raw.json", collected)
    write_json(Path(directory) / "calibration.json", calibration)
    return calibration


def measure_panel(
    run, builds, cases, estimator, directory, fixture_root, calibration,
    measurement_protocol, guarded=True,
):
    results = []
    for regime in ("normal", "rerun"):
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
        write_json(Path(directory) / f"{regime}.json", bundle)
        result = cost_guard(bundle, calibration, run["run_id"])
        results.append(result)
        if not guarded or not result["needs_rerun"]:
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
        if not (path / "normal.json").exists() and not any(r["id"] == id_ for r in evidence):
            continue
        calibration = run["calibrations"]["cost"][name]
        try:
            if not (path / "normal.json").exists():
                raise Incomplete("Missing normal-count cost bundle")
            normal = read_json(path / "normal.json")

            def check(bundle, cases=cases, calibration=calibration):
                if bundle["case_hashes"] != {c["case_id"]: digest(c) for c in cases}:
                    raise Incomplete("Cost bundle case definitions changed")
                if any(
                    bundle["arms"][a]["build_id"] != run["builds"][a]["id"]
                    for a in ("baseline", "control", "evolved")
                ):
                    raise Incomplete("Cost bundle build identities changed")
                return cost_guard(bundle, calibration, run["run_id"], historical=True)

            if normal["regime"] != "normal":
                raise Incomplete("Expected the normal-count bundle first")
            result = check(normal)
            if name in guarded and result["needs_rerun"]:
                if not (path / "rerun.json").exists():
                    raise Incomplete("Cost breach requires a fresh doubled-count rerun")
                rerun = read_json(path / "rerun.json")
                if rerun["regime"] != "rerun" or rerun["session_id"] == normal["session_id"]:
                    raise Incomplete("Rerun must use a fresh session and doubled counts")
                result = check(rerun)
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
