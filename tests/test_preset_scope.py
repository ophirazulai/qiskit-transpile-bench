from datetime import UTC, datetime
from types import SimpleNamespace

from qtb.canonical import digest, write_json
from qtb.coordinator.calibration import measure_costs, preflight, required_cost_panels
from qtb.coordinator.costs import replay_costs
from qtb.errors import Incomplete
from qtb.evaluator import record, verdict


def comparison(scope, paths=()):
    cases = [
        {"case_id": "timing", "panel": "timing", "modes": ["timing_e2e"]},
        {"case_id": "preset", "panel": "preset", "modes": ["preset_build"]},
    ]
    run = {"profile": "iterations-profile", "scope": {"2": scope}, "changed_paths": paths}
    return SimpleNamespace(run=run, manifest={"cases": cases})


def test_preset_is_measured_but_required_only_for_relevant_scope():
    optimization = comparison({"stages": ["init", "optimization"], "unmapped_paths": []})
    assert set(required_cost_panels(optimization)) == {"timing"}
    unknown = comparison({"stages": ["init", "optimization"], "unmapped_paths": ["x.py"]})
    assert "preset" in required_cost_panels(unknown)
    target = comparison(
        {"stages": ["init", "optimization"], "unmapped_paths": []},
        ["qiskit/transpiler/target.py"],
    )
    assert "preset" in required_cost_panels(target)


def test_replayed_preset_breach_is_report_only_when_scope_is_unrelated(tmp_path, monkeypatch):
    comparison_ = comparison({"stages": ["init", "optimization"], "unmapped_paths": []})
    case = comparison_.manifest["cases"][1]
    run = comparison_.run
    run.update(
        run_id="run",
        builds={arm: {"id": "build"} for arm in ("baseline", "control", "evolved")},
        calibrations={"cost": {"preset": {}}},
    )
    write_json(
        tmp_path / "cost/preset/normal.json",
        {
            "regime": "normal",
            "session_id": "normal",
            "case_hashes": {"preset": digest(case)},
            "arms": {arm: {"build_id": "build"} for arm in run["builds"]},
        },
    )
    monkeypatch.setattr(
        "qtb.coordinator.costs.cost_guard",
        lambda *_args, **_kwargs: {"result": "failed", "needs_rerun": True},
    )
    evidence = replay_costs(tmp_path, run, {"cases": [case]}, [])
    assert evidence[0]["result"] == "not_evaluated"
    assert evidence[0]["observed_result"] == "failed"
    assert verdict([record("IA2/improvement", "improvement", "passed"), *evidence],
                   ["IA2/improvement"]) == "PASS"


def test_unrelated_preset_is_still_measured_and_recorded(tmp_path, monkeypatch):
    comparison_ = comparison({"stages": ["init", "optimization"], "unmapped_paths": []})
    comparison_.run.update(
        calibrations={"cost": {"timing": {}, "preset": {}}}, builds={}
    )
    comparison_.policy = {"measurement_protocol": {}}
    comparison_.directory = tmp_path
    comparison_.fixtures = tmp_path
    comparison_.prefix = "IA"
    comparison_.progress = lambda *_: None
    comparison_.records = []
    comparison_.evidence = comparison_.records.append
    measured = []

    def fake_measure(_run, _builds, cases, _estimator, _directory, _fixtures, _calibration,
                     _protocol,
                     guarded=True):
        measured.append((cases[0]["panel"], guarded))
        return {"result": "failed", "needs_rerun": True}

    monkeypatch.setattr("qtb.coordinator.calibration.measure_panel", fake_measure)
    measure_costs(comparison_)
    assert measured == [("timing", True), ("preset", False)]
    preset = next(row for row in comparison_.records if row["id"] == "IA5/preset")
    assert preset["result"] == "not_evaluated"
    assert preset["observed_result"] == "failed"


def test_unrelated_preset_measurement_failure_does_not_block(tmp_path, monkeypatch):
    comparison_ = comparison({"stages": ["init", "optimization"], "unmapped_paths": []})
    comparison_.run.update(calibrations={"cost": {"timing": {}, "preset": {}}}, builds={})
    comparison_.policy = {"measurement_protocol": {}}
    comparison_.directory = comparison_.fixtures = tmp_path
    comparison_.prefix = "IA"
    comparison_.progress = lambda *_: None
    comparison_.records = []
    comparison_.evidence = comparison_.records.append

    def fake_measure(_run, _builds, cases, *_args, **_kwargs):
        if cases[0]["panel"] == "preset":
            raise Incomplete("measurement unavailable")
        return {"result": "passed", "needs_rerun": False}

    monkeypatch.setattr("qtb.coordinator.calibration.measure_panel", fake_measure)
    measure_costs(comparison_)
    preset = next(row for row in comparison_.records if row["id"] == "IA5/preset")
    assert preset["result"] == "not_evaluated"
    assert preset["observed_result"] == "unresolved"


def test_cached_calibration_ignores_report_only_preset_failure(tmp_path, monkeypatch):
    comparison_ = comparison({"stages": ["init", "optimization"], "unmapped_paths": []})
    comparison_.root = tmp_path
    comparison_.records = []
    comparison_.save = lambda: None
    comparison_.evidence = comparison_.records.append
    monkeypatch.setattr("qtb.coordinator.calibration.calibration_key", lambda _: "key")
    write_json(
        tmp_path / "calibrations/key/summary.json",
        {
            "created_at": datetime.now(UTC).isoformat(),
            "quality": {
                "false_rejection_rate": 0.0,
                "noisy_guard_count": 0,
                "freeze_allowed": True,
                "role_failures": [],
            },
            "cost": {
                "timing": {"null_rejections": [False], "freeze_allowed": True},
                "preset": {"null_rejections": [True], "freeze_allowed": False},
            },
            "false_rejection": {"freeze_allowed": False},
        },
    )
    summary = preflight(comparison_, [])
    assert summary["false_rejection"]["freeze_allowed"] is True
    assert {row["id"]: row["result"] for row in comparison_.records}["calibration/cost"] == "passed"
