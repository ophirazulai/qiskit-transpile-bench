import math

import pytest

from qtb.errors import HarnessError, Incomplete
from qtb.evaluator import record, verdict
from qtb.evaluator.statistics import estimate, hierarchical_weights, paired_panel


def test_worked_example():
    baseline = [[400, 420, 380, 410], [1500, 1550, 1480, 1600], [1850, 1800, 1900, 1820]]
    evolved = [[390, 400, 385, 395], [1490, 1500, 1500, 1540], [1800, 1810, 1830, 1790]]
    cases = [dict(case_id=str(i), weight=1 / 3) for i in range(3)]
    rows = {
        (str(c), r, s): {"D2": values[c][s]}
        for c in range(3)
        for s in range(4)
        for r, values in [("baseline", baseline), ("evolved", evolved)]
    }
    result = paired_panel(cases, rows, "D2", list(range(4)))
    assert result["score"] == pytest.approx(0.9803, abs=0.00005)
    assert result["SE"] == pytest.approx(0.00584, abs=0.000005)
    assert result["ln_score_plus_2SE"] == pytest.approx(-0.00820, abs=0.000005)


def test_pairing_preserves_covariance():
    assert estimate([0.1, -0.1, 0.1, -0.1])["SE"] > 0
    cases = [dict(case_id="a", weight=0.5), dict(case_id="b", weight=0.5)]
    rows = {
        (c, r, s): {"D2": math.exp(d if r == "evolved" else 0)}
        for c, values in [("a", [0.1, -0.1]), ("b", [-0.1, 0.1])]
        for s, d in enumerate(values)
        for r in ("baseline", "evolved")
    }
    assert paired_panel(cases, rows, "D2", [0, 1])["SE"] == 0


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_invalid_positive_metrics_never_reach_logs(bad):
    rows = {
        ("a", r, s): {"D2": bad if r == "evolved" else 1}
        for r in ("baseline", "evolved")
        for s in range(2)
    }
    with pytest.raises(Incomplete):
        paired_panel([{"case_id": "a", "weight": 1}], rows, "D2", [0, 1])


def test_missing_seed_is_not_dropped():
    with pytest.raises(Incomplete):
        paired_panel([{"case_id": "a", "weight": 1}], {}, "D2", [0, 1])


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([record("q/improvement", "improvement", "passed")], "PASS"),
        ([record("q/improvement", "improvement", "failed")], "NO_IMPROVEMENT"),
        (
            [record("q/improvement", "improvement", "failed"), record("guard", "guard", "failed")],
            "CONSTRAINT_VIOLATION",
        ),
        (
            [
                record("q/improvement", "improvement", "passed"),
                record("ref", "correctness", "failed", "reference"),
            ],
            "INCONCLUSIVE",
        ),
        (
            [
                record("q/improvement", "improvement", "passed"),
                record("ref", "correctness", "failed", "reference"),
                record("candidate", "correctness", "failed"),
            ],
            "CONSTRAINT_VIOLATION",
        ),
        (
            [record("q/improvement", "improvement", "passed"), record("h", "harness", "failed")],
            "ERROR",
        ),
        ([], "INCONCLUSIVE"),
    ],
)
def test_verdict_precedence(rows, expected):
    assert verdict(rows, ["q/improvement"]) == expected


def test_verdict_refuses_vacuity_duplicates_and_quality_rerun():
    with pytest.raises(HarnessError):
        verdict([], [])
    with pytest.raises(HarnessError):
        verdict([record("q/improvement", "improvement", "passed")] * 2, ["q/improvement"])
    with pytest.raises(HarnessError):
        verdict([record("q/improvement", "guard", "passed_on_rerun")], ["q/improvement"])
    assert (
        verdict([record("q/improvement", "improvement", "passed")], ["q/improvement", "missing"])
        == "INCONCLUSIVE"
    )


def test_weights_are_hierarchical():
    cases = [
        dict(
            case_id=str(i),
            family="G1" if i < 4 else "G2",
            optimization_level=i % 2,
            size_band="small",
            topology="line",
            native_basis="cx",
            input_group=str(i),
            variant="numeric",
        )
        for i in range(6)
    ]
    weights = hierarchical_weights(cases)
    assert sum(weights[str(i)] for i in range(4)) == 0.5
    assert sum(weights.values()) == 1
