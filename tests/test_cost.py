from datetime import UTC, datetime

import pytest

from qtb.errors import HarnessError, Incomplete
from qtb.evaluator.cost import calibrate_cost, cost_estimate, cost_guard, validate_bundle


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
