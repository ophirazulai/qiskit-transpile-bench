import pytest

from stage_world import make_world


@pytest.fixture(autouse=True)
def private_runner_lock(monkeypatch, tmp_path):
    """Unit tests never contend with a real comparison's machine-wide runner lock."""
    from qtb import coordinator
    from qtb.coordinator import costs

    lock = tmp_path / "qtb-runner-test.lock"
    monkeypatch.setattr(costs, "runner_lock", lambda: lock)
    monkeypatch.setattr(coordinator, "runner_lock", lambda: lock)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Sources, a store and fake stage work (``stage_world``)."""
    return make_world(tmp_path, monkeypatch)


def write_session(root, manifest, policy, rows, evidence, states, run=None):
    """A session directory as the stages leave it, for ``decide`` and ``clean`` tests.

    ``evidence`` maps a stage to its evidence records and ``states`` a stage to its state
    fields (at least ``status``; the quality stage also ``gate``).
    """
    from pathlib import Path

    from qtb.canonical import canonical_bytes, digest, write_json
    from qtb.config import STAGES

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    scope = {"stages": [], "components": [], "unmapped_paths": []}
    run = {
        "run_id": "synthetic-run",
        "profile": manifest["profile"],
        "hashes": {"manifest": digest(manifest), "policy": digest(policy)},
        "builds": {"baseline": {"id": "base"}, "evolved": {"id": "idea"}},
        "scope": {str(level): dict(scope) for level in range(4)},
        "changed_paths": [],
        **(run or {}),
    }
    write_json(root / "run.json", run)
    write_json(root / "manifest.json", manifest)
    write_json(root / "policy.json", policy)
    by_case = {c["case_id"]: c for c in manifest["cases"]}
    lines = []
    for row in rows:
        case = by_case[row["case_id"]]
        reference = (
            case["circuit"]["sha256"]
            if case["semantic_reference"]["kind"] == "input"
            else case["semantic_reference"]["sha256"]
        )
        row = dict(row)
        row.setdefault("id", digest([row["case_id"], row["revision"], row["seed"]]))
        row.setdefault(
            "checks",
            [
                {
                    "oracle": "C6",
                    "status": "verified",
                    "reference_hash": reference,
                    "input_domain": case["input_domain"],
                    "covers": list(STAGES),
                    "substituted": [],
                }
            ],
        )
        lines.append(canonical_bytes(row) + b"\n")
    (root / "observations.jsonl").write_bytes(b"".join(lines))
    for stage, fields in states.items():
        write_json(
            root / "stages" / stage / "state.json",
            {"format": "qtb-stage/1", "stage": stage, "seconds": 1, **fields},
        )
        write_json(root / "stages" / stage / "evidence.json", evidence.get(stage, []))
    return root
