import math
from copy import deepcopy

import pytest

from qtb.canonical import digest, read_json, write_json
from qtb.errors import HarnessError, Incomplete
from qtb.evaluator.cost import (
    cost_estimate,
    cost_guard,
    regime_counts,
    thresholds,
    thresholds_id,
    validate_bundle,
)

POLICY = {
    "measurement_protocol": {
        "screen_rounds": 2,
        "timing_rounds": 4,
        "rerun_multiplier": 2,
        "companion_rounds": 3,
        "memory_processes": 5,
        "warmups": 1,
        "minimum_calls": 2,
        "minimum_ns": 1_000_000_000,
    },
    "cost_thresholds": {
        "panel_ratio": 1.03,
        "case_ratio": 1.10,
        "case_floor_ns": 25_000_000,
        "case_floor_bytes": 33_554_432,
        "screen_fraction": 0.5,
    },
}
UNSCREENED = deepcopy(POLICY)
UNSCREENED["measurement_protocol"].update(screen_rounds=0, timing_rounds=2)


def test_regime_counts_follow_the_policy_and_reject_nonsense():
    protocol = POLICY["measurement_protocol"]
    assert regime_counts("timing") == {"normal": 10, "rerun": 20}
    assert regime_counts("timing", protocol) == {"screen": 2, "normal": 4, "rerun": 8}
    assert regime_counts("timing", dict(protocol, screen_rounds=0)) == {"normal": 4, "rerun": 8}
    assert regime_counts("companion", protocol) == {"normal": 3, "rerun": 6}
    assert regime_counts("memory", protocol) == {"normal": 5, "rerun": 10}
    with pytest.raises(HarnessError):
        regime_counts("timing", dict(protocol, screen_rounds=4))
    with pytest.raises(HarnessError):
        regime_counts("unknown")


def test_thresholds_are_fixed_by_policy():
    screen = thresholds(POLICY, "timing", "screen")
    full = thresholds(POLICY, "timing", "normal")
    assert screen["count"] == 2 and full["count"] == 4
    assert math.isclose(full["noise_panel"], math.log(1.03))
    assert math.isclose(screen["noise_panel"], 0.5 * math.log(1.03))
    assert full["case_ratio"] == 1.10 and full["floor"] == 25_000_000
    assert thresholds(POLICY, "memory", "rerun")["floor"] == 33_554_432
    with pytest.raises(Incomplete):
        thresholds(UNSCREENED, "timing", "screen")
    assert thresholds_id(POLICY) == thresholds_id(deepcopy(POLICY))
    assert thresholds_id(POLICY) != thresholds_id(
        {"cost_thresholds": dict(POLICY["cost_thresholds"], panel_ratio=1.05)}
    )


def test_distinct_estimators():
    assert cost_estimate([[1, 2, 100], [3, 4, 5], [10, 11, 12]], "timing") == 4
    assert cost_estimate([1, 2, 100, 4, 5], "memory") == 4
    samples = {str(i): [[i + 1], [i + 1], [i + 1]] for i in range(20)}
    assert cost_estimate(samples, "companion") == 10.5


def bundle(ratios, regime="normal", policy=POLICY, session="session", baseline_ns=10**9):
    """A two-arm timing bundle; ``ratios`` maps case → evolved/baseline ratio."""
    count = regime_counts("timing", policy["measurement_protocol"])[regime]
    return dict(
        complete=True,
        run_id="run",
        session_id=session,
        regime=regime,
        estimator="timing",
        machine={},
        thresholds_id=thresholds_id(policy),
        arms={
            arm: dict(
                arm_id=f"{session}-{arm}",
                session_id=session,
                build_id="identical",
                samples={
                    case: [[int(baseline_ns * (ratio if arm == "evolved" else 1))]] * count
                    for case, ratio in ratios.items()
                },
            )
            for arm in ("baseline", "evolved")
        },
    )


def weights(ratios):
    return {case: 1 / len(ratios) for case in ratios}


def test_fresh_baseline_prevents_historical_100ms_hiding_92ms_regression():
    slow = bundle({"c": 1.15})
    assert cost_guard(slow, POLICY, "timing", weights({"c": 1}), "run")["needs_rerun"]
    with pytest.raises(Incomplete):
        validate_bundle(slow, POLICY, "timing", weights({"c": 1}), run_id="new-run")
    slow["arms"]["baseline"]["session_id"] = "old"
    with pytest.raises(HarnessError):
        validate_bundle(slow, POLICY, "timing", weights({"c": 1}))


@pytest.mark.parametrize(
    "ratio,regime,expected",
    [
        (1.15, "normal", "unresolved"),
        (1.15, "rerun", "failed"),
        (1.0, "rerun", "passed_on_rerun"),
        (1.0, "normal", "passed"),
    ],
)
def test_rerun(ratio, regime, expected):
    result = cost_guard(bundle({"c": ratio}, regime), POLICY, "timing", weights({"c": 1}))
    assert result["result"] == expected
    assert result["threshold"] == thresholds(POLICY, "timing", regime)


def test_identical_builds_cannot_coalesce_arms():
    same = bundle({"c": 1.15})
    same["arms"]["evolved"]["arm_id"] = same["arms"]["baseline"]["arm_id"]
    with pytest.raises(HarnessError):
        validate_bundle(same, POLICY, "timing", weights({"c": 1}))


def test_bundle_must_match_the_policy_thresholds_and_estimator():
    other = deepcopy(POLICY)
    other["cost_thresholds"]["panel_ratio"] = 1.05
    with pytest.raises(Incomplete, match="different thresholds"):
        cost_guard(bundle({"c": 1.0}), other, "timing", weights({"c": 1}))
    with pytest.raises(Incomplete, match="estimator"):
        cost_guard(bundle({"c": 1.0}), POLICY, "memory", weights({"c": 1}))
    with pytest.raises(Incomplete, match="not part of the protocol"):
        cost_guard(bundle({"c": 1.0}, "screen"), UNSCREENED, "timing", weights({"c": 1}))


def test_floors_protect_millisecond_cases_from_jitter():
    # A 15% wobble on a 1 ms case is 0.15 ms, far under the 25 ms floor; a 12%
    # slowdown of a 1 s case is 120 ms and counts.
    mixed = bundle({"tiny": 1.0, "big": 1.0})
    mixed["arms"]["evolved"]["samples"]["tiny"] = [[1_150_000]] * 4
    mixed["arms"]["baseline"]["samples"]["tiny"] = [[1_000_000]] * 4
    mixed["arms"]["evolved"]["samples"]["big"] = [[1_120_000_000]] * 4
    result = cost_guard(mixed, POLICY, "timing", weights({"tiny": 1, "big": 1}))
    assert result["candidate"]["cap_breaches"] == ["big"]
    only_tiny = bundle({"tiny": 1.0, "big": 1.0})
    only_tiny["arms"]["evolved"]["samples"]["tiny"] = [[1_150_000]] * 4
    only_tiny["arms"]["baseline"]["samples"]["tiny"] = [[1_000_000]] * 4
    only_tiny["arms"]["evolved"]["samples"]["big"] = [[1_000_000_000]] * 4
    only_tiny["arms"]["baseline"]["samples"]["big"] = [[1_000_000_000]] * 4
    result = cost_guard(only_tiny, POLICY, "timing", weights({"tiny": 1, "big": 1}))
    assert result["candidate"]["cap_breaches"] == []


def test_screen_must_clear_a_fraction_of_the_full_band():
    w = weights({"c": 1})
    clean = cost_guard(bundle({"c": 1.0}, "screen"), POLICY, "timing", w)
    assert (clean["result"], clean["regime"], clean["count"]) == ("passed", "screen", 2)
    assert not clean["needs_full"] and not clean["needs_rerun"]
    # Inside the full band (3%) but outside the screen's half band.
    near = cost_guard(bundle({"c": 1.02}, "screen"), POLICY, "timing", w)
    assert near["result"] == "unresolved" and near["needs_full"] and not near["needs_rerun"]
    assert cost_guard(bundle({"c": 1.02}, "normal"), POLICY, "timing", w)["result"] == "passed"
    slow = cost_guard(bundle({"c": 1.15}, "screen"), POLICY, "timing", w)
    assert slow["result"] == "unresolved" and slow["needs_full"]


def measured_panel(monkeypatch, tmp_path, ratio):
    from qtb.coordinator import costs

    calls = []

    def worker(build, job, directory):
        calls.append((build["id"], job["mode"], directory))
        scale = ratio if build["id"] == "evolved" else 1.0
        return [
            {"seed": seed, "status": "ok", "samples_ns": [int(10**9 * scale)]}
            for seed in job["seeds"]
        ]

    monkeypatch.setattr(costs, "run_worker", worker)
    monkeypatch.setattr(costs.os, "getloadavg", lambda: (0, 0, 0))
    case = {"case_id": "c", "panel": "timing", "timeout_s": 120, "modes": ["timing_e2e"]}
    builds = {arm: {"id": arm} for arm in ("baseline", "evolved")}
    run = {"run_id": "run", "machine": {}}
    result = costs.measure_panel(
        run, builds, [case], "timing", tmp_path / "cost/timing", tmp_path, POLICY
    )
    return result, calls, case, builds


def test_clear_screen_ends_the_panel_early(monkeypatch, tmp_path):
    result, calls, *_ = measured_panel(monkeypatch, tmp_path, 1.0)
    assert (result["result"], result["regime"], result["count"]) == ("passed", "screen", 2)
    assert len(calls) == 2 * 2  # screen rounds × arms, nothing more
    saved = read_json(tmp_path / "cost/timing/screen.json")
    assert saved["regime"] == "screen" and saved["thresholds_id"] == thresholds_id(POLICY)
    assert not (tmp_path / "cost/timing/normal.json").exists()


def test_unclear_screen_measures_in_full_then_reruns_fresh(monkeypatch, tmp_path):
    result, calls, *_ = measured_panel(monkeypatch, tmp_path, 1.15)
    assert (result["result"], result["regime"], result["count"]) == ("failed", "rerun", 8)
    assert len(calls) == (2 + 4 + 8) * 2
    regimes = ("screen", "normal", "rerun")
    bundles = {r: read_json(tmp_path / f"cost/timing/{r}.json") for r in regimes}
    assert len({b["session_id"] for b in bundles.values()}) == 3
    assert [len(b["arms"]["evolved"]["samples"]["c"]) for b in bundles.values()] == [2, 4, 8]


def test_replay_accepts_a_clear_screen_and_demands_the_rest_otherwise(monkeypatch, tmp_path):
    from qtb.coordinator.costs import replay_costs

    for ratio, expected in ((1.0, "passed"), (1.15, "failed")):
        directory = tmp_path / f"ratio-{ratio}"
        _, _, case, builds = measured_panel(monkeypatch, directory, ratio)
        run = dict(profile="iterations-profile", run_id="run", scope={}, builds=builds)
        replayed = replay_costs(directory, run, {"cases": [case]}, POLICY, [])
        assert replayed[0]["result"] == expected
        assert replayed[0]["regime"] == ("screen" if ratio == 1.0 else "rerun")
        if ratio == 1.15:
            (directory / "cost/timing/rerun.json").unlink()
            replayed = replay_costs(directory, run, {"cases": [case]}, POLICY, [])
            assert replayed[0]["result"] == "unresolved"
            (directory / "cost/timing/normal.json").unlink()
            replayed = replay_costs(directory, run, {"cases": [case]}, POLICY, [])
            assert "Missing normal" in replayed[0]["detail"]


def test_replay_refuses_a_pre_threshold_archive(monkeypatch, tmp_path):
    from qtb.coordinator.costs import replay_costs

    _, _, case, builds = measured_panel(monkeypatch, tmp_path, 1.0)
    run = dict(profile="iterations-profile", run_id="run", scope={}, builds=builds)
    legacy = {"measurement_protocol": POLICY["measurement_protocol"]}
    with pytest.raises(HarnessError, match="pre-version-4"):
        replay_costs(tmp_path, run, {"cases": [case]}, legacy, [])


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
    builds = {arm: {"id": str(i)} for i, arm in enumerate(("baseline", "evolved"))}
    result = costs.collect_panel(
        {"run_id": "r", "machine": {}}, builds, cases, "timing", tmp_path, 3, list(builds),
        tmp_path, POLICY["measurement_protocol"],
    )
    assert len(calls) == 3 * 2  # rounds × arms, not rounds × arms × cases
    assert len({directory for _, _, directory in calls}) == 6
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
    builds = {arm: {"id": str(i)} for i, arm in enumerate(("baseline", "evolved"))}
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
    assert len(calls) == 4  # two rounds × two arms, rather than 80 processes
    assert all(
        mode == "timing_reuse" and seeds == tuple(range(20))
        for _, mode, seeds, _, _ in calls
    )
    assert len({directory for _, _, _, directory, _ in calls}) == 4
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
    builds = {arm: {"id": arm} for arm in ("baseline", "evolved")}
    protocol = POLICY["measurement_protocol"]

    def collect(run_id, name):
        order.clear()
        result = costs.collect_panel(
            {"run_id": run_id, "machine": {}}, builds, [case], "timing",
            tmp_path / name, 5, list(builds), tmp_path, protocol,
        )
        return result, list(order)

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
    case = {"case_id": "c", "timeout_s": 120, "modes": ["timing_e2e"]}
    builds = {arm: {"id": arm} for arm in ("baseline", "evolved")}
    result = costs.measure_panel(
        {"run_id": "run", "machine": {}}, builds, [case], "timing", panel, tmp_path, UNSCREENED
    )
    saved = read_json(panel / "normal.json")
    assert result["result"] == "passed"
    assert sorted(calls) == sorted(list(builds) * 2)
    assert saved["complete"] and saved["session_id"] != "interrupted"
    assert all(len(saved["arms"][arm]["samples"]["c"]) == 2 for arm in builds)


def test_cost_replay_ignores_claimed_pass_without_fresh_rerun(tmp_path):
    from qtb.coordinator.costs import replay_costs
    from qtb.evaluator import record

    case = {"case_id": "c", "panel": "timing", "modes": ["timing_e2e"]}
    normal = dict(bundle({"c": 1.15}, "normal", UNSCREENED), case_hashes={"c": digest(case)})
    run = dict(
        profile="iterations-profile",
        run_id="run",
        scope={},
        builds={a: {"id": "identical"} for a in ("baseline", "evolved")},
    )
    write_json(tmp_path / "cost/timing/normal.json", normal)
    forged = [record("IA5/timing", "cost", "passed")]
    result = replay_costs(tmp_path, run, {"cases": [case]}, UNSCREENED, forged)
    assert result[0]["result"] == "unresolved"
    rerun = dict(
        bundle({"c": 1.15}, "rerun", UNSCREENED, session="fresh"), case_hashes=normal["case_hashes"]
    )
    write_json(tmp_path / "cost/timing/rerun.json", rerun)
    result = replay_costs(tmp_path, run, {"cases": [case]}, UNSCREENED, forged)
    assert result[0]["result"] == "failed"
    stale = dict(rerun, session_id="session")
    for arm in stale["arms"].values():
        arm["session_id"] = "session"
    write_json(tmp_path / "cost/timing/rerun.json", stale)
    result = replay_costs(tmp_path, run, {"cases": [case]}, UNSCREENED, forged)
    assert result[0]["result"] == "unresolved" and "fresh session" in result[0]["detail"]
