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


def test_empty_confirm_report_summary_still_writes_decision(tmp_path):
    from qtb.reporter import make_decision, write_report

    manifest, policy, rows, evidence = synthetic("confirm-profile")
    policy["iterations_groups"] = sorted({c["input_group"] for c in manifest["cases"]})
    records, required, summaries = evaluate_quality(manifest, policy, rows, evidence)
    assert summaries["leave_iterations_out"]["status"] == "unavailable"
    decision = make_decision(
        {"profile": "confirm-profile", "hashes": {"manifest": "m", "policy": "p"},
         "run_id": "test"},
        records, required, summaries, rows,
    )
    write_report(tmp_path, decision)
    assert (tmp_path / "decision.json").exists()
    assert (tmp_path / "report.md").exists()


def test_empty_leave_family_out_is_unresolved():
    manifest, policy, rows, evidence = synthetic("confirm-profile")
    for case in manifest["cases"]:
        if case["role"] == "scored":
            case["family"] = "G1"
    records, _, summaries = evaluate_quality(manifest, policy, rows, evidence)
    breadth = next(r for r in records if r["id"] == "CA3/breadth")
    assert breadth["result"] == "unresolved"
    assert summaries["leave_family_out/G1"]["status"] == "unavailable"


def split_evidence(evidence, profile="iterations-profile"):
    """The synthetic passing evidence, as each stage would have written it."""
    prefix = "CA" if profile == "confirm-profile" else "IA"
    owner = {
        "harness/roundtrip": "compile",
        "audit/determinism": "quality",
        f"{prefix}1/C0": "quality",
        f"{prefix}1/C6": "quality",
        f"{prefix}6/completeness": "quality",
        "CA1/C1-lite": "quality",
    }
    stages = {}
    for row in evidence:
        if row["id"].endswith("/stage-coverage"):
            continue  # decide computes it
        stage = owner.get(row["id"]) or ("cost" if "5/" in row["id"] else "correctness")
        stages.setdefault(stage, []).append(row)
    return stages


@pytest.mark.parametrize("guard_regression", [False, True])
def test_decide_replays_synthetic_session_to_known_verdict(tmp_path, guard_regression, monkeypatch):
    from conftest import write_session
    from qtb.canonical import read_json
    from qtb.coordinator.decide import decide

    monkeypatch.setattr("qtb.coordinator.costs.replay_costs", lambda *args: args[-1])
    manifest, policy, rows, evidence = synthetic("iterations-profile")
    if guard_regression:
        guard = next(
            c for c in manifest["cases"]
            if c["role"] == "guard" and c["native_basis"] == "cx"
        )
        baseline = {
            row["seed"]: row["D2"] for row in rows
            if row["case_id"] == guard["case_id"] and row["revision"] == "baseline"
        }
        for row in rows:
            if row["case_id"] == guard["case_id"] and row["revision"] == "evolved":
                row["D2"] = baseline[row["seed"]] * 1.08
    complete = {"status": "complete"}
    states = {
        "compile": complete,
        "quality": dict(complete, gate="closed" if guard_regression else "improved"),
    }
    if not guard_regression:
        states.update(correctness=complete, cost=complete)
    write_session(tmp_path, manifest, policy, rows, split_evidence(evidence), states)
    code, decision = decide(tmp_path, progress=lambda *_: None)
    assert decision["status"] == ("CONSTRAINT_VIOLATION" if guard_regression else "PASS")
    assert code == (20 if guard_regression else 0)
    assert read_json(tmp_path / "decision.json") == decision
    if guard_regression:
        assert any(
            record["id"].endswith(f"cap/{guard['case_id']}/D2")
            and record["result"] == "failed"
            for record in decision["constraints"]
        )
        assert any("not checked" in note for note in decision["notes"])


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


def test_scope_is_conservative_and_level_aware():
    assert set(changed_scope(["crates/transpiler/src/passes/sabre/layout.rs"], 2)["stages"]) == {
        "layout",
        "routing",
    }
    assert "optimization" in changed_scope(["qiskit/transpiler/passes/layout/vf2.py"], 3)["stages"]
    assert len(changed_scope(["unknown/new.py"], 2)["stages"]) == 6


def test_non_source_changes_do_not_widen_stage_scope():
    non_source = [
        "test/python/transpiler/test_sabre.py",
        "crates/transpiler/tests/vf2.rs",
        "releasenotes/notes/vf2-change.rst",
        "docs/transpiler/vf2.md",
        ".github/workflows/test.yml",
        "Cargo.lock",
        "CHANGELOG.md",
    ]
    result = changed_scope(["crates/transpiler/src/passes/sabre/layout.rs", *non_source], 2)
    assert result["stages"] == ["layout", "routing"]
    assert result["unmapped_paths"] == []
    assert changed_scope(non_source, 2)["stages"] == []
    assert len(changed_scope(["examples/vf2_demo.py"], 2)["stages"]) == 6


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


def test_unstable_metrics_cannot_establish_quality_violations():
    manifest, policy, rows, evidence = synthetic(ratio=1.2)
    for row in evidence:
        if row["id"] == "audit/determinism":
            row["result"] = "unresolved"
    records, required, _ = evaluate_quality(manifest, policy, rows, evidence)
    assert verdict(records, required) == "INCONCLUSIVE"
