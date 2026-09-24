from types import SimpleNamespace

from qtb.coordinator import Comparison
from qtb.coordinator.storage import append_record
from qtb.evaluator.scope import covered


def _case(level=1):
    return {
        "case_id": "scored/example/L1",
        "role": "scored",
        "seeds_per_block": 1,
        "optimization_level": level,
        "circuit": {"sha256": "input-hash"},
        "clifford_variant": {"sha256": "variant-hash"},
        "semantic_reference": {"kind": "input"},
        "input_domain": "all_zero",
    }


def _c7(mode="full"):
    return {
        "oracle": "C7",
        "case_id": "scored/example/L1",
        "revision": "evolved",
        "mode": mode,
        "seed": 0,
        "status": "verified",
        "reference_hash": "variant-hash",
        "input_domain": "all_inputs",
        "covers": ["init", "layout", "routing", "translation", "optimization", "scheduling"]
        if mode == "full"
        else ["init", "layout", "routing", "translation"],
        "substituted": [] if mode == "full" else ["unitary_synthesis"],
    }


def test_c7_variant_contract_and_substitution_are_enforced():
    case = _case()
    full = _c7()
    assert covered(case, [full], {"stages": ["optimization"], "components": []})
    assert not covered(
        case,
        [dict(full, reference_hash="wrong")],
        {"stages": ["optimization"], "components": []},
    )
    prefix = _c7("prefix")
    assert covered(case, [prefix], {"stages": ["translation"], "components": []})
    assert not covered(
        case,
        [prefix],
        {"stages": ["translation"], "components": ["unitary_synthesis"]},
    )
    assert not covered(case, [prefix], {"stages": ["optimization"], "components": []})


def test_aggregate_stage_coverage_reads_c7_results(tmp_path):
    case = _case()
    observations = [
        {
            "case_id": case["case_id"],
            "revision": revision,
            "seed_block": "B0",
            "checks": [
                {"oracle": oracle, "status": "verified", "covers": []}
                for oracle in ("C0", "C6")
            ],
        }
        for revision in ("baseline", "evolved")
    ]
    records = []
    comparison = SimpleNamespace(
        directory=tmp_path,
        run={
            "profile": "iterations-profile",
            "scope": {"1": {"stages": ["optimization"], "components": []}},
        },
        prefix="IA",
        evidence=records.append,
    )

    Comparison.aggregate_checks(comparison, [case], observations)
    assert records[-1]["result"] == "unresolved"

    append_record(tmp_path / "clifford.jsonl", _c7())
    records.clear()
    Comparison.aggregate_checks(comparison, [case], observations)
    assert records[-1]["id"] == "IA1/stage-coverage"
    assert records[-1]["result"] == "passed"
