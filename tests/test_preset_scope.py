from types import SimpleNamespace

from qtb.canonical import digest, write_json
from qtb.config import STAGES
from qtb.coordinator.costs import cost_panels, measure_costs, replay_costs, required_cost_panels
from qtb.errors import Incomplete
from qtb.evaluator import record, verdict

POLICY = {
    "measurement_protocol": {"screen_rounds": 0, "timing_rounds": 2, "rerun_multiplier": 2},
    "cost_thresholds": {
        "panel_ratio": 1.03,
        "case_ratio": 1.10,
        "case_floor_ns": 25_000_000,
        "case_floor_bytes": 33_554_432,
        "screen_fraction": 0.5,
    },
}


def comparison(scope, paths=()):
    cases = [
        {"case_id": "timing", "panel": "timing", "modes": ["timing_e2e"]},
        {"case_id": "preset", "panel": "preset", "modes": ["preset_build"]},
    ]
    run = {"profile": "iterations-profile", "scope": {"2": scope}, "changed_paths": paths}
    return SimpleNamespace(run=run, manifest={"cases": cases})


def scoped(stages, unmapped=(), profile="iterations-profile"):
    cases = [
        {"case_id": "T1", "panel": "timing", "modes": ["timing_e2e"]},
        {"case_id": "T11", "panel": "timing", "modes": ["timing_reuse"]},
        {"case_id": "T12", "panel": "timing-basis", "modes": ["timing_reuse"]},
        {"case_id": "preset/cz", "panel": "preset", "modes": ["preset_build"]},
    ]
    run = {"profile": profile, "scope": {"2": {"stages": stages, "unmapped_paths": list(unmapped)}}}
    return SimpleNamespace(run=run, manifest={"cases": cases})


def test_basis_twins_and_companion_follow_the_change_scope():
    def names(comparison):
        panels = cost_panels(comparison)
        return {name: [c["case_id"] for c in cases] for name, (cases, _) in panels.items()}

    routing = names(scoped(["layout", "routing"]))
    assert "timing-basis" not in routing
    assert routing["timing"] == ["T1", "T11"] and routing["companion"] == ["T11"]
    translation = names(scoped(["translation"]))
    assert translation["timing-basis"] == ["T12"] and "companion" not in translation
    assert "timing-basis" in names(scoped(["optimization"]))
    scheduling = names(scoped(["scheduling"]))
    assert set(scheduling) == {"timing", "preset"}
    unknown = names(scoped(["scheduling"], unmapped=["mystery.py"]))
    assert {"timing-basis", "companion", "preset"} <= set(unknown)
    everything = names(scoped(list(STAGES)))
    assert {"timing-basis", "companion"} <= set(everything)
    # Whatever is measured under a relevant scope is guarded.
    assert "timing-basis" in required_cost_panels(scoped(["translation"]))
    assert "companion" in required_cost_panels(scoped(["routing"]))
    assert "preset" not in required_cost_panels(scoped(["translation"]))


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
    run.update(run_id="run", builds={arm: {"id": "build"} for arm in ("baseline", "evolved")})
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
    evidence = replay_costs(tmp_path, run, {"cases": [case]}, POLICY, [])
    assert evidence[0]["result"] == "not_evaluated"
    assert evidence[0]["observed_result"] == "failed"
    assert verdict([record("IA2/improvement", "improvement", "passed"), *evidence],
                   ["IA2/improvement"]) == "PASS"


def prepared(tmp_path):
    comparison_ = comparison({"stages": ["init", "optimization"], "unmapped_paths": []})
    comparison_.run.update(builds={})
    comparison_.policy = POLICY
    comparison_.directory = comparison_.fixtures = tmp_path
    comparison_.prefix = "IA"
    comparison_.progress = lambda *_: None
    comparison_.records = []
    comparison_.evidence = comparison_.records.append
    return comparison_


def test_unrelated_preset_is_still_measured_and_recorded(tmp_path, monkeypatch):
    comparison_ = prepared(tmp_path)
    measured = []

    def fake_measure(_run, _builds, cases, _estimator, _directory, _fixtures, policy,
                     guarded=True):
        assert policy is POLICY
        measured.append((cases[0]["panel"], guarded))
        return {"result": "failed", "needs_rerun": True}

    monkeypatch.setattr("qtb.coordinator.costs.measure_panel", fake_measure)
    measure_costs(comparison_)
    assert measured == [("timing", True), ("preset", False)]
    preset = next(row for row in comparison_.records if row["id"] == "IA5/preset")
    assert preset["result"] == "not_evaluated"
    assert preset["observed_result"] == "failed"


def test_unrelated_preset_measurement_failure_does_not_block(tmp_path, monkeypatch):
    comparison_ = prepared(tmp_path)

    def fake_measure(_run, _builds, cases, *_args, **_kwargs):
        if cases[0]["panel"] == "preset":
            raise Incomplete("measurement unavailable")
        return {"result": "passed", "needs_rerun": False}

    monkeypatch.setattr("qtb.coordinator.costs.measure_panel", fake_measure)
    measure_costs(comparison_)
    preset = next(row for row in comparison_.records if row["id"] == "IA5/preset")
    assert preset["result"] == "not_evaluated"
    assert preset["observed_result"] == "unresolved"
