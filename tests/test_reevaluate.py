import pytest

from qtb.canonical import read_json, write_json
from qtb.config import load_profile
from qtb.errors import HarnessError
from qtb.reevaluate import evaluate_run


def test_evaluate_replays_with_changed_coordinator_without_overwriting_archive(tmp_path):
    manifest, policy, hashes = load_profile("iterations-profile", verify=False)
    hashes["coordinator"] = "archived-harness-identity"
    run = {
        "format": "qtb-run/1",
        "run_id": "old-run",
        "profile": "iterations-profile",
        "hashes": hashes,
        "builds": {},
    }
    write_json(tmp_path / "run.json", run)
    write_json(tmp_path / "manifest.json", manifest)
    write_json(tmp_path / "policy.json", policy)
    write_json(tmp_path / "evidence.json", [])
    write_json(tmp_path / "decision.json", {"archived": True})
    (tmp_path / "report.md").write_text("Archived report\n")
    original = (tmp_path / "decision.json").read_bytes()
    original_report = (tmp_path / "report.md").read_bytes()

    output, decision = evaluate_run(tmp_path)

    assert decision["status"] == "INCONCLUSIVE"
    assert decision["hashes"]["coordinator"] == "archived-harness-identity"
    assert decision["reevaluation"]["evaluator_identity"] != "archived-harness-identity"
    assert output.parent == tmp_path / "reevaluations"
    assert read_json(output / "decision.json") == decision
    assert (output / "report.md").exists()
    assert (tmp_path / "decision.json").read_bytes() == original
    assert (tmp_path / "report.md").read_bytes() == original_report
    assert read_json(tmp_path / "run.json") == run


def test_evaluate_rejects_modified_archived_policy(tmp_path):
    manifest, policy, hashes = load_profile("iterations-profile", verify=False)
    write_json(tmp_path / "run.json", {"profile": policy["profile"], "hashes": hashes})
    write_json(tmp_path / "manifest.json", manifest)
    policy["rng_seed"] += 1
    write_json(tmp_path / "policy.json", policy)

    with pytest.raises(HarnessError, match="Archived policy hash mismatch"):
        evaluate_run(tmp_path)
    assert not (tmp_path / "reevaluations").exists()
