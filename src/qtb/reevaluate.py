"""Recompute a decision from archived evidence without changing the original run."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

from qtb.canonical import digest, file_hash, read_json
from qtb.config import coordinator_identity
from qtb.coordinator.costs import replay_costs
from qtb.coordinator.storage import read_records
from qtb.errors import HarnessError
from qtb.evaluator import evaluate_quality
from qtb.reporter import make_decision, write_report


def evaluate_run(directory):
    directory = Path(directory).resolve()
    run = read_json(directory / "run.json")
    manifest = read_json(directory / "manifest.json")
    policy = read_json(directory / "policy.json")
    if manifest.get("format") != "qtb-manifest/1" or policy.get("format") != "qtb-policy/1":
        raise HarnessError("Unknown archived profile format")
    if run["profile"] != manifest["profile"] or run["profile"] != policy["profile"]:
        raise HarnessError("Archived profile identity mismatch")
    for name, value in (("manifest", manifest), ("policy", policy)):
        if run["hashes"][name] != digest(value):
            raise HarnessError(f"Archived {name} hash mismatch")

    evidence_file = directory / "evidence.json"
    evidence = read_json(evidence_file) if evidence_file.exists() else []
    evidence = replay_costs(directory, run, manifest, policy, evidence)
    rows = read_records(directory / "observations.jsonl")
    records, required, summaries = evaluate_quality(manifest, policy, rows, evidence)
    if "scope" in run:
        from types import SimpleNamespace

        from qtb.coordinator.costs import required_cost_panels

        prefix = "CA" if run["profile"] == "confirm-profile" else "IA"
        panels = required_cost_panels(SimpleNamespace(run=run, manifest=manifest))
        required = sorted(set(required) | {f"{prefix}5/{name}" for name in panels})
    decision = make_decision(run, records, required, summaries, rows, manifest, policy)
    original = directory / "decision.json"
    decision["reevaluation"] = {
        "evaluator_identity": coordinator_identity(),
        "source_decision_sha256": file_hash(original) if original.exists() else None,
        "evaluated_at": datetime.now(UTC).isoformat(),
    }
    output = directory / "reevaluations" / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    )
    write_report(output, decision)
    return output, decision
