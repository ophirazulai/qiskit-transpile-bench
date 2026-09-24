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
