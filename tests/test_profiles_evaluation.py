import copy

import pytest

from qtb.config import load_profile
from qtb.evaluator import evaluate_quality, record, verdict
from qtb.evaluator.scope import changed_scope, covered


def synthetic(profile="iterations-profile", ratio=0.97):
    manifest, policy, _ = load_profile(profile)
    policy = copy.deepcopy(policy)
    policy["bootstrap_replicates"] = 30
    rows = []
    for case in manifest["cases"]:
        if case["role"] in {"timing", "memory"}:
            continue
        for seed in range(case["seeds_per_block"]):
            baseline = case.get("expected", dict(D2=100 + seed, N2=200 + seed))
            if case["role"] == "zero_baseline":
                baseline = dict(D2=0, N2=0)
            if case["role"] == "deterministic":
                baseline = dict(D2=100, N2=200)
            for revision in ("baseline", "evolved"):
                metrics = dict(baseline)
                if revision == "evolved" and case["role"] in {"scored", "guard"}:
                    metrics = {k: v * ratio for k, v in metrics.items()}
                rows.append(
                    dict(
                        case_id=case["case_id"],
                        revision=revision,
                        seed=seed,
                        seed_block="B0",
                        mode="quality",
                        **metrics,
                    )
                )
    evidence = [
        record(id_, "harness" if id_.startswith("harness/") else "completeness", "passed")
        for id_ in policy["required_ids"]
        if not id_.endswith("/improvement") and id_ != "CA3/breadth"
    ]
    return manifest, policy, rows, evidence


def test_confirm_manifest_weight_contract():
    manifest, _, _ = load_profile("confirm-profile")
    scored = [c for c in manifest["cases"] if c["role"] == "scored"]
    assert len(scored) == 133
    assert len({c["input_group"] for c in scored}) == 38
    for group, expected in [
        ("qft_n100", 1 / 96),
        ("qv_n50_d50", 1 / 128),
        ("mcx_kg24_n16", 1 / 384),
    ]:
        assert (
            next(
                c["weight"]
                for c in scored
                if c["input_group"] == group and c["optimization_level"] == 2
            )
            == expected
        )
    assert all("split" not in c for c in manifest["cases"])
    assert len(manifest["replacements"]) == 6


@pytest.mark.parametrize("profile", ["iterations-profile", "confirm-profile"])
def test_synthetic_gain_walks_pass_path(profile):
    manifest, policy, rows, evidence = synthetic(profile)
    records, required, _ = evaluate_quality(manifest, policy, rows, evidence)
    assert verdict(records, required) == "PASS"


def test_a_a_has_zero_effect_and_never_passes():
    manifest, policy, rows, evidence = synthetic(ratio=1.0)
    records, required, summaries = evaluate_quality(manifest, policy, rows, evidence)
    assert verdict(records, required) == "NO_IMPROVEMENT"
    assert summaries["IA2/improvement"]["score"] == 1
    assert summaries["IA2/improvement"]["SE"] == 0


def test_guard_failure_outranks_no_improvement():
    manifest, policy, rows, evidence = synthetic(ratio=1.0)
    for row in rows:
        if row["revision"] == "evolved" and "/heavy_hex_d9_cx/" in row["case_id"]:
            row["D2"] *= 1.08
    records, required, _ = evaluate_quality(manifest, policy, rows, evidence)
    assert verdict(records, required) == "CONSTRAINT_VIOLATION"


@pytest.mark.parametrize("mutation", ["missing", "zero", "audit"])
def test_incomplete_or_unstable_panel_cannot_claim_improvement(mutation):
    manifest, policy, rows, evidence = synthetic()
    if mutation == "missing":
        rows.pop(0)
    if mutation == "zero":
        rows[0]["D2"] = 0
    if mutation == "audit":
        for row in evidence:
            if row["id"] == "audit/determinism":
                row["result"] = "unresolved"
    records, required, _ = evaluate_quality(manifest, policy, rows, evidence)
    assert verdict(records, required) == "INCONCLUSIVE"


def test_scope_is_conservative_level_aware_and_widen_only():
    assert set(changed_scope(["crates/transpiler/src/passes/sabre/layout.rs"], 2)["stages"]) == {
        "layout",
        "routing",
    }
    assert "optimization" in changed_scope(["qiskit/transpiler/passes/layout/vf2.py"], 3)["stages"]
    assert len(changed_scope(["unknown/new.py"], 2, ["layout"])["stages"]) == 6
    assert (
        "optimization"
        in changed_scope(["crates/transpiler/src/passes/sabre/layout.rs"], 2, ["optimization"])[
            "stages"
        ]
    )


def test_coverage_cannot_use_substituted_components_or_wrong_contract():
    case = {
        "semantic_reference": {"kind": "input"},
        "circuit": {"sha256": "a"},
        "input_domain": "all_inputs",
    }
    check = dict(
        status="verified",
        reference_hash="a",
        input_domain="all_zero",
        covers=["init"],
        substituted=[],
    )
    scope = dict(stages=["init"], components=[])
    assert not covered(case, [check], scope)
    check["input_domain"] = "all_inputs"
    assert covered(case, [check], scope)
    check["substituted"] = ["unitary_synthesis"]
    scope["components"] = ["unitary_synthesis"]
    assert not covered(case, [check], scope)
