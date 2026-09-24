from datetime import UTC, datetime

import pytest

from qtb.errors import HarnessError, Incomplete
from qtb.evaluator.cost import (
    calibrate_cost,
    cost_estimate,
    cost_guard,
    regime_counts,
    validate_bundle,
)

SCREENED = {
    "screen_rounds": 2,
    "timing_rounds": 4,
    "rerun_multiplier": 2,
    "calibration_rounds": 8,
    "warmups": 1,
    "minimum_calls": 2,
    "minimum_ns": 1_000_000_000,
}


def test_regime_counts_follow_the_policy_and_reject_nonsense():
    assert regime_counts("timing") == ({"normal": 10, "rerun": 20}, 30)
    assert regime_counts("timing", SCREENED) == ({"screen": 2, "normal": 4, "rerun": 8}, 8)
    assert regime_counts("timing", dict(SCREENED, screen_rounds=0)) == (
        {"normal": 4, "rerun": 8},
        8,
    )
    assert regime_counts("companion", {"companion_rounds": 3, "calibration_rounds": 30}) == (
        {"normal": 3, "rerun": 6},
        30,
    )
    assert regime_counts("memory", {"memory_processes": 5}) == ({"normal": 5, "rerun": 10}, 10)
    with pytest.raises(HarnessError):
        regime_counts("timing", dict(SCREENED, screen_rounds=4))
    with pytest.raises(HarnessError):
        regime_counts("timing", dict(SCREENED, calibration_rounds=6))


def test_distinct_estimators():
    assert cost_estimate([[1, 2, 100], [3, 4, 5], [10, 11, 12]], "timing") == 4
    assert cost_estimate([1, 2, 100, 4, 5], "memory") == 4
    samples = {str(i): [[i + 1], [i + 1], [i + 1]] for i in range(20)}
    assert cost_estimate(samples, "companion") == 10.5


def test_memory_calibration_resamples_with_replacement():
    arms = {a: {"case": list(range(100, 110))} for a in ("baseline", "control")}
    result = calibrate_cost(
        arms, "memory", {"case": 1}, {"cpu": "test"}, datetime.now(UTC).isoformat(), replicates=100
    )
    assert result["regimes"]["normal"]["floors"]["case"] > 0
    assert result["regimes"]["rerun"]["floors"]["case"] > 0
    assert result == calibrate_cost(
        arms, "memory", {"case": 1}, {"cpu": "test"}, result["created_at"], replicates=100
    )


def bundle_and_calibration(ratio=1.15, control=1.0, regime="normal"):
    now = datetime.now(UTC).isoformat()
    calibration = calibrate_cost(
        {a: {"c": [[80]] * 30} for a in ("baseline", "control")},
        "timing",
        {"c": 1},
        {"cpu": "test"},
        now,
        replicates=20,
    )
    bundle = dict(
        complete=True,
        run_id="run",
        session_id="session",
        regime=regime,
        machine=calibration["machine"],
        calibration_id=calibration["id"],
        measured_at=now,
        arms={
            arm: dict(
                arm_id=arm,
                session_id="session",
                build_id="identical",
                samples={"c": [[80 * value]] * (20 if regime == "rerun" else 10)},
            )
            for arm, value in [("baseline", 1), ("control", control), ("evolved", ratio)]
        },
    )
    return bundle, calibration


def test_fresh_baseline_prevents_historical_100ms_hiding_92ms_regression():
    bundle, calibration = bundle_and_calibration()
    assert cost_guard(bundle, calibration, "run")["needs_rerun"]
    with pytest.raises(Incomplete):
        validate_bundle(bundle, calibration, run_id="new-run")
    bundle["arms"]["baseline"]["session_id"] = "old"
    with pytest.raises(HarnessError):
        validate_bundle(bundle, calibration)


@pytest.mark.parametrize(
    "ratio,control,regime,expected",
    [
        (1.15, 1.0, "normal", "unresolved"),
        (1.15, 1.0, "rerun", "failed"),
        (1.0, 1.0, "rerun", "passed_on_rerun"),
        (1.15, 1.15, "rerun", "unresolved"),
        (1.0, 1.0, "normal", "passed"),
    ],
)
def test_rerun_and_control(ratio, control, regime, expected):
    bundle, calibration = bundle_and_calibration(ratio, control, regime)
    assert cost_guard(bundle, calibration)["result"] == expected


def test_identical_builds_cannot_coalesce_arms():
    bundle, calibration = bundle_and_calibration()
    bundle["arms"]["control"]["arm_id"] = "baseline"
    with pytest.raises(HarnessError):
        validate_bundle(bundle, calibration)


def test_cost_null_calibration_applies_confirmed_rerun_rule():
    arms = {a: {"case": [[100]] * 30} for a in ("baseline", "control")}
    result = calibrate_cost(
        arms, "timing", {"case": 1}, {}, datetime.now(UTC).isoformat(), replicates=100
    )
    assert result["false_rejection_rate"] == 0
    assert result["null_rejections"] == [False] * 100


def screened_bundle(calibration, ratio, regime, session="session"):
    count = calibration["regimes"][regime]["count"]
    return dict(
        complete=True,
        run_id="run",
        session_id=session,
        regime=regime,
        machine=calibration["machine"],
        calibration_id=calibration["id"],
        measured_at=calibration["created_at"],
        arms={
            arm: dict(
                arm_id=f"{session}-{arm}",
                session_id=session,
                build_id="identical",
                samples={"c": [[80 * value]] * count},
            )
            for arm, value in [("baseline", 1), ("control", 1), ("evolved", ratio)]
        },
    )


def test_screen_regime_is_calibrated_and_judged_against_the_full_band():
    arms = {a: {"c": [[80]] * 8} for a in ("baseline", "control")}
    calibration = calibrate_cost(
        arms, "timing", {"c": 1}, {"cpu": "test"}, datetime.now(UTC).isoformat(),
        replicates=20, protocol=SCREENED,
    )
    assert [r["count"] for r in calibration["regimes"].values()] == [2, 4, 8]
    assert calibration["collected"] == 8
    clean = cost_guard(screened_bundle(calibration, 1.0, "screen"), calibration)
    assert (clean["result"], clean["regime"], clean["count"]) == ("passed", "screen", 2)
    assert not clean["needs_full"] and not clean["needs_rerun"]
    slow = cost_guard(screened_bundle(calibration, 1.15, "screen"), calibration)
    assert slow["result"] == "unresolved" and slow["needs_full"] and not slow["needs_rerun"]
    # A regime the calibration never bootstrapped cannot be judged.
    with pytest.raises(Incomplete):
        cost_guard(
            dict(screened_bundle(calibration, 1.0, "screen"), regime="normal"), calibration
        )
    unscreened = calibrate_cost(
        arms, "timing", {"c": 1}, {"cpu": "test"}, calibration["created_at"],
        replicates=20, protocol=dict(SCREENED, screen_rounds=0),
    )
    assert list(unscreened["regimes"]) == ["normal", "rerun"]
    stray = dict(screened_bundle(calibration, 1.0, "screen"), calibration_id=unscreened["id"])
    with pytest.raises(Incomplete):
        cost_guard(stray, unscreened)


def measured_panel(monkeypatch, tmp_path, ratio):
    from qtb.coordinator import costs

    calls = []

    def worker(build, job, directory):
        calls.append((build["id"], job["mode"], directory))
        scale = ratio if build["id"] == "evolved" else 1.0
        return [
            {"seed": seed, "status": "ok", "samples_ns": [int(80 * scale)]}
            for seed in job["seeds"]
        ]

    monkeypatch.setattr(costs, "run_worker", worker)
    monkeypatch.setattr(costs.os, "getloadavg", lambda: (0, 0, 0))
    case = {"case_id": "c", "panel": "timing", "timeout_s": 120, "modes": ["timing_e2e"]}
    builds = {arm: {"id": arm} for arm in ("baseline", "control", "evolved")}
    arms = {a: {"c": [[80]] * 8} for a in ("baseline", "control")}
    calibration = calibrate_cost(
        arms, "timing", {"c": 1}, {}, datetime.now(UTC).isoformat(),
        replicates=20, protocol=SCREENED,
    )
    run = {"run_id": "run", "machine": {}}
    result = costs.measure_panel(
        run, builds, [case], "timing", tmp_path / "cost/timing", tmp_path, calibration,
        SCREENED,
    )
    return result, calls, case, calibration, builds


def test_clear_screen_ends_the_panel_early(monkeypatch, tmp_path):
    from qtb.canonical import read_json

    result, calls, *_ = measured_panel(monkeypatch, tmp_path, 1.0)
    assert (result["result"], result["regime"], result["count"]) == ("passed", "screen", 2)
    assert len(calls) == 2 * 3  # screen rounds × arms, nothing more
    assert (tmp_path / "cost/timing/screen.json").exists()
    assert not (tmp_path / "cost/timing/normal.json").exists()
    assert read_json(tmp_path / "cost/timing/screen.json")["regime"] == "screen"


def test_unclear_screen_measures_in_full_then_reruns_fresh(monkeypatch, tmp_path):
    from qtb.canonical import read_json

    result, calls, *_ = measured_panel(monkeypatch, tmp_path, 1.15)
    assert (result["result"], result["regime"], result["count"]) == ("failed", "rerun", 8)
    assert len(calls) == (2 + 4 + 8) * 3
    regimes = ("screen", "normal", "rerun")
    bundles = {r: read_json(tmp_path / f"cost/timing/{r}.json") for r in regimes}
    assert len({b["session_id"] for b in bundles.values()}) == 3
    assert [len(b["arms"]["evolved"]["samples"]["c"]) for b in bundles.values()] == [2, 4, 8]


def test_replay_accepts_a_clear_screen_and_demands_the_rest_otherwise(monkeypatch, tmp_path):
    from qtb.coordinator.costs import replay_costs

    for ratio, expected in ((1.0, "passed"), (1.15, "failed")):
        directory = tmp_path / f"ratio-{ratio}"
        _, _, case, calibration, builds = measured_panel(monkeypatch, directory, ratio)
        run = dict(
            profile="iterations-profile", run_id="run", scope={},
            calibrations={"cost": {"timing": calibration}}, builds=builds,
        )
        replayed = replay_costs(directory, run, {"cases": [case]}, [])
        assert replayed[0]["result"] == expected
        assert replayed[0]["regime"] == ("screen" if ratio == 1.0 else "rerun")
        if ratio == 1.15:
            (directory / "cost/timing/rerun.json").unlink()
            replayed = replay_costs(directory, run, {"cases": [case]}, [])
            assert replayed[0]["result"] == "unresolved"
            (directory / "cost/timing/normal.json").unlink()
            replayed = replay_costs(directory, run, {"cases": [case]}, [])
            assert "Missing normal" in replayed[0]["detail"]


def test_timing_panel_is_one_process_per_round_and_arm(monkeypatch, tmp_path):
    from qtb.coordinator import costs

    calls = []

    def worker(build, job, directory):
        calls.append((build["id"], job, directory))
        return [
            {"seed": index, "status": "ok", "samples_ns": [index + 10 * int(build["id"])]}
            for index in job["seeds"]
        ]

    monkeypatch.setattr(costs, "run_worker", worker)
    monkeypatch.setattr(costs.os, "getloadavg", lambda: (0, 0, 0))
    cases = [
        {"case_id": "T1", "timeout_s": 120, "modes": ["timing_e2e"], "timing": {"fixed_seed": 7}},
        {"case_id": "T11", "timeout_s": 300, "modes": ["timing_reuse"], "timing": {"fixed_seed": 3}},  # noqa: E501
        {"case_id": "preset/cz", "timeout_s": 120, "modes": ["preset_build"]},
    ]
    builds = {arm: {"id": str(i)} for i, arm in enumerate(("baseline", "control", "evolved"))}
    result = costs.collect_panel(
        {"run_id": "r", "machine": {}}, builds, cases, "timing", tmp_path, 3, list(builds),
        tmp_path, SCREENED,
    )
    assert len(calls) == 3 * 3  # rounds × arms, not rounds × arms × cases
    assert len({directory for _, _, directory in calls}) == 9
    for _, job, _ in calls:
        assert job["mode"] == "timing_batch" and "case" not in job
        assert job["seeds"] == [0, 1, 2] and job["timeout_s"] == 1500
        assert [(e["case"]["case_id"], e["mode"], e["seed"]) for e in job["batch"]] == [
            ("T1", "timing_e2e", 7), ("T11", "timing_reuse", 3), ("preset/cz", "preset_build", 0),
        ]
        assert (job["warmups"], job["minimum_calls"], job["minimum_ns"]) == (1, 2, 10**9)
    for arm, build in builds.items():
        samples = result["arms"][arm]["samples"]
        assert samples == {
            c["case_id"]: [[index + 10 * int(build["id"])]] * 3 for index, c in enumerate(cases)
        }


def test_companion_batches_seeds_per_arm_round_without_changing_samples(monkeypatch, tmp_path):
    from qtb.coordinator import costs

    calls = []

    def worker(build, job, directory):
        calls.append((build["id"], job["mode"], tuple(job["seeds"]), directory, job))
        return [
            {"seed": seed, "status": "ok", "samples_ns": [seed + int(build["id"]) + 1]}
            for seed in job["seeds"]
        ]

    monkeypatch.setattr(costs, "run_worker", worker)
    monkeypatch.setattr(costs.os, "getloadavg", lambda: (0, 0, 0))
    case = {"case_id": "c", "timeout_s": 120, "modes": ["timing_reuse"]}
    builds = {arm: {"id": str(i)} for i, arm in enumerate(("baseline", "control", "evolved"))}
    result = costs.collect_panel(
        {"run_id": "r", "machine": {}},
        builds,
        [case],
        "companion",
        tmp_path,
        2,
        list(builds),
        tmp_path,
        {"warmups": 2, "minimum_calls": 4, "minimum_ns": 250},
    )
    assert len(calls) == 6  # two rounds × three arms, rather than 120 processes
    assert all(
        mode == "timing_reuse" and seeds == tuple(range(20))
        for _, mode, seeds, _, _ in calls
    )
    assert len({directory for _, _, _, directory, _ in calls}) == 6
    assert all(
        (job["warmups"], job["minimum_calls"], job["minimum_ns"]) == (2, 4, 250)
        for *_, job in calls
    )
    assert result["timing_protocol"] == {"warmups": 2, "minimum_calls": 4, "minimum_ns": 250}
    for arm, build in builds.items():
        samples = result["arms"][arm]["samples"]["c"]
        assert samples == {
            str(seed): [[seed + int(build["id"]) + 1]] * 2 for seed in range(20)
        }


def test_cost_interleaving_seed_is_derived_from_run_and_archived(monkeypatch, tmp_path):
    from qtb.coordinator import costs

    order = []

    def worker(build, job, directory):
        order.append(build["id"])
        return [{"seed": job["seeds"][0], "status": "ok", "samples_ns": [1]}]

    monkeypatch.setattr(costs, "run_worker", worker)
    monkeypatch.setattr(costs.os, "getloadavg", lambda: (0, 0, 0))
    case = {"case_id": "c", "timeout_s": 120, "modes": ["timing_e2e"]}
    builds = {arm: {"id": arm} for arm in ("baseline", "control", "evolved")}
    protocol = {"warmups": 1, "minimum_calls": 3, "minimum_ns": 1_000_000_000}

    def collect(run_id, name):
        order.clear()
        bundle = costs.collect_panel(
            {"run_id": run_id, "machine": {}}, builds, [case], "timing",
            tmp_path / name, 5, list(builds), tmp_path, protocol,
        )
        return bundle, list(order)

    first, first_order = collect("run-one", "first")
    second, second_order = collect("run-one", "second")
    third, _ = collect("run-two", "third")
    assert first_order == second_order
    assert first["interleaving_seed"] == second["interleaving_seed"]
    assert first["interleaving_seed"] == costs.interleaving_seed(
        "run-one", [case], "timing", 5, list(builds)
    )
    assert third["interleaving_seed"] != first["interleaving_seed"]


def test_incomplete_cost_panel_restarts_all_arms(monkeypatch, tmp_path):
    from qtb.canonical import read_json, write_json
    from qtb.coordinator import costs

    panel = tmp_path / "panel"
    panel.mkdir()
    write_json(panel / "normal.json", {"complete": False, "session_id": "interrupted"})
    calls = []

    def worker(build, job, directory):
        calls.append(build["id"])
        return [{"seed": job["seeds"][0], "status": "ok", "samples_ns": [100]}]

    monkeypatch.setattr(costs, "run_worker", worker)
    monkeypatch.setattr(costs.os, "getloadavg", lambda: (0, 0, 0))
    monkeypatch.setattr(costs, "cost_guard", lambda *_: {"result": "passed", "needs_rerun": False})
    case = {"case_id": "c", "timeout_s": 120, "modes": ["timing_e2e"]}
    builds = {arm: {"id": arm} for arm in ("baseline", "control", "evolved")}
    calibration = {"id": "cal", "regimes": {"normal": {"count": 2}, "rerun": {"count": 4}}}
    protocol = {"warmups": 1, "minimum_calls": 3, "minimum_ns": 1_000_000_000}
    result = costs.measure_panel(
        {"run_id": "run", "machine": {}}, builds, [case], "timing", panel,
        tmp_path, calibration, protocol,
    )
    saved = read_json(panel / "normal.json")
    assert result["result"] == "passed"
    assert sorted(calls) == sorted(list(builds) * 2)
    assert saved["complete"] and saved["session_id"] != "interrupted"
    assert all(len(saved["arms"][arm]["samples"]["c"]) == 2 for arm in builds)


def test_cost_replay_ignores_claimed_pass_without_fresh_rerun(tmp_path):
    from qtb.canonical import digest, write_json
    from qtb.coordinator.costs import replay_costs
    from qtb.evaluator import record

    normal, calibration = bundle_and_calibration()
    case = {"case_id": "c", "panel": "timing"}
    normal["case_hashes"] = {"c": digest(case)}
    run = dict(
        profile="iterations-profile",
        run_id="run",
        scope={},
        calibrations={"cost": {"timing": calibration}},
        builds={a: {"id": "identical"} for a in ("baseline", "control", "evolved")},
    )
    write_json(tmp_path / "cost/timing/normal.json", normal)
    forged = [record("IA5/timing", "cost", "passed")]
    result = replay_costs(tmp_path, run, {"cases": [case]}, forged)
    assert result[0]["result"] == "unresolved"
    rerun, _ = bundle_and_calibration(regime="rerun")
    rerun.update(
        calibration_id=calibration["id"], session_id="fresh", case_hashes=normal["case_hashes"]
    )
    for arm in rerun["arms"].values():
        arm["session_id"] = "fresh"
    write_json(tmp_path / "cost/timing/rerun.json", rerun)
    result = replay_costs(tmp_path, run, {"cases": [case]}, forged)
    assert result[0]["result"] == "failed"
