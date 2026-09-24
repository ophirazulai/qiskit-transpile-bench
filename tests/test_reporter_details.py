import math

from qtb.evaluator import record
from qtb.reporter import make_decision, render_report


def test_report_uses_archived_quality_and_zero_baseline_evidence():
    panel = {
        "score": 0.81,
        "ln_score": math.log(0.81),
        "SE": 0.01,
        "ln_score_plus_2SE": math.log(0.81) + 0.02,
        "cases": {"scored": {"worst_seed": 1}},
        "seeds": [0, 1],
    }
    summaries = {
        "CA2/improvement": panel,
        "CA3/primary/N2": {**panel, "score": 1.21},
        "CA4/family/G1/D2": panel,
        "CA4/optimization_level/2/D2": panel,
        "CA4/cap/scored/D2": panel,
        "instance_bootstrap": {
            "status": "reported", "U_instance": -0.1, "group_counts": {"G1": 3}
        },
    }
    fingerprint_a = {"layout": [{"pass": "OldPass", "budget": {"trials": 1}}]}
    fingerprint_b = {"layout": [{"pass": "NewPass", "budget": {"trials": 2}}]}
    observations = [
        {"case_id": "scored", "revision": "baseline", "seed": 0,
         "seed_block": "B0", "mode": "quality", "fingerprint": fingerprint_a},
        {"case_id": "scored", "revision": "evolved", "seed": 0,
         "seed_block": "B0", "mode": "quality", "fingerprint": fingerprint_b},
    ]
    for seed, evolved in ((0, 0), (1, 1)):
        for revision, value in (("baseline", 0), ("evolved", evolved)):
            observations.append(
                {"case_id": "zero", "revision": revision, "seed": seed,
                 "seed_block": "B0", "mode": "quality", "D2": value, "N2": value}
            )
    records = [
        record("CA2/improvement", "improvement", "passed"),
        record("CA1/stage-coverage", "correctness", "unresolved", cases=["scored"]),
    ]
    decision = make_decision(
        {"profile": "confirm-profile", "hashes": {}, "run_id": "r",
         "calibrations": {"false_rejection": {"noisy_guard_count": 3, "combined": 0.02,
                                              "quality": 0.01, "cost": 0.01}}},
        records,
        ["CA2/improvement", "CA1/stage-coverage"],
        summaries,
        observations,
        {"cases": [{"case_id": "zero", "role": "zero_baseline", "seeds_per_block": 2}]},
    )
    report = render_report(decision)

    assert decision["objective"][0]["score"] == 0.81
    assert math.isclose(decision["trade_score"], 0.99)
    assert decision["zero_baseline_deltas"]["cases"][0]["worst_seed"] == 1
    assert decision["noisy_guard_count"] == 3
    assert decision["calibrated_false_rejection_rate"] == 0.02
    assert "| family | G1 | D2 |" in report
    assert "| optimization_level | 2 | D2 |" in report
    assert "| zero | D2 | 1 | 1 | 0 | 1 |" in report
    assert "Input groups by family: G1: 3." in report
    assert "Uncovered scored cases: scored." in report
    assert "OldPass" in report and "NewPass" in report
    assert "No frozen exclusions artifact was recorded" in report


def test_missing_evidence_is_marked_unavailable():
    decision = make_decision(
        {"profile": "iterations-profile", "hashes": {}, "run_id": "r"},
        [record("IA2/improvement", "improvement", "unresolved")],
        ["IA2/improvement"],
        {},
        [],
        {"cases": [{"case_id": "zero", "role": "zero_baseline", "seeds_per_block": 2}]},
    )
    assert decision["objective"] == []
    assert decision["trade_score"] is None
    assert all(row["status"] == "unavailable" for row in decision["zero_baseline_deltas"]["cases"])
    assert "Primary D2 estimate: unavailable" in render_report(decision)
