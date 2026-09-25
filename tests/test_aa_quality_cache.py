from qtb.canonical import write_json
from qtb.config import case_hash
from qtb.coordinator import Comparison
from qtb.coordinator.storage import (
    cache_quality_observation,
    invalidate_quality_entry,
    quality_cache_key,
)
from qtb.coordinator.store import Store

PROTOCOL = {"quality_batch_size": 2}
CASE = {
    "case_id": "aa",
    "role": "scored",
    "seeds_per_block": 3,
    "target": {"file": "target.json", "sha256": "target"},
    "circuit": {"sha256": "circuit"},
    "semantic_reference": {"kind": "input"},
    "logical_qubits": 1,
    "options": {},
    "constraint_form": "target",
    "input_domain": "all_zero",
}


def _setup(tmp_path, monkeypatch, builds, stored_seeds=(0,)):
    session = tmp_path / "session"
    fixtures = tmp_path / "fixtures"
    session.mkdir()
    fixtures.mkdir()
    write_json(
        fixtures / "target.json", {"num_qubits": 1, "native_2q_names": [], "instructions": []}
    )
    store = Store(tmp_path / "store")
    key = quality_cache_key(builds["baseline"], CASE, {}, PROTOCOL, "implementation")
    entry = store.quality_dir(key)
    output = tmp_path / "old-output.gz"
    job_file = tmp_path / "old-job.json"
    output.write_bytes(b"old output")
    job_file.write_text("{}")
    for seed in stored_seeds:
        cache_quality_observation(
            entry,
            seed,
            {
                "case_id": "aa",
                "case_hash": case_hash(CASE),
                "build_id": builds["baseline"]["id"],
                "revision": "baseline",
                "seed": seed,
                "seed_block": "B0",
                "worker": {"job_file": str(job_file), "output": str(output)},
                "output": str(output),
                "checks": [{"status": "verified"}],
            },
        )

    comparison = Comparison.__new__(Comparison)
    comparison.directory = session
    comparison.stage = "quality"
    comparison.store = store
    comparison.machine = {}
    comparison.records = []
    comparison.fixtures = fixtures
    comparison.run = {
        "builds": builds,
        "hashes": {"implementation": "implementation"},
        "profile": "iterations-profile",
    }
    comparison.policy = {"measurement_protocol": PROTOCOL}
    comparison.progress = lambda *_: None
    comparison.routing_batch = lambda *_: {}
    comparison.check_routing = lambda *_: None
    calls = []

    def job(revision, _case, _mode, seeds):
        calls.append((revision, tuple(seeds)))
        path = session / f"{revision}-{seeds[0]}-output.gz"
        path.write_bytes(revision.encode())
        return [
            {
                "seed": seed,
                "status": "ok",
                "output": str(path),
                "output_hash": "hash",
                "layout": None,
                "job_file": str(job_file),
            }
            for seed in seeds
        ]

    comparison.job = job
    monkeypatch.setattr(
        "qtb.coordinator.structural_result",
        lambda *_: {"status": "verified", "output_hash": "hash", "D2": 0, "N2": 0},
    )
    return comparison, entry, key, calls


def test_identical_builds_compile_both_arms_despite_stored_baseline_rows(tmp_path, monkeypatch):
    build = {"id": "same-build"}
    comparison, entry, _, calls = _setup(
        tmp_path, monkeypatch, {"baseline": build, "evolved": build}
    )

    rows = comparison.quality([CASE])
    # Batches compile concurrently; only the committed rows keep plan order.
    assert sorted(calls) == [
        ("baseline", (0, 1)),
        ("baseline", (2,)),
        ("evolved", (0, 1)),
        ("evolved", (2,)),
    ]
    assert [row["revision"] for row in rows] == ["baseline"] * 3 + ["evolved"] * 3
    assert all(not row.get("cached", False) for row in rows)
    # A/A never reads or adds stored rows.
    assert sorted(p.name for p in entry.glob("[0-9].json")) == ["0.json"]


def test_baseline_rows_come_from_the_store_and_evolved_rows_never_go_there(
    tmp_path, monkeypatch
):
    builds = {"baseline": {"id": "base"}, "evolved": {"id": "idea"}}
    comparison, entry, key, calls = _setup(tmp_path, monkeypatch, builds)

    rows = comparison.quality([CASE])
    assert sorted(calls) == [("baseline", (1, 2)), ("evolved", (0, 1)), ("evolved", (2,))]
    cached = [row for row in rows if row.get("cached")]
    assert [(r["revision"], r["seed"], r["cached_from"]) for r in cached] == [
        ("baseline", 0, key)
    ]
    # Fresh baseline rows are added to the store; evolved rows stay in the session.
    assert sorted(p.name for p in entry.glob("[0-9].json")) == ["0.json", "1.json", "2.json"]
    assert not any("idea" in p.read_text() for p in entry.glob("[0-9].json"))
    assert [p for p in comparison.store.root.rglob("*") if "idea" in p.name] == []


def test_invalidated_baseline_rows_are_neither_read_nor_extended(tmp_path, monkeypatch):
    builds = {"baseline": {"id": "base"}, "evolved": {"id": "idea"}}
    comparison, entry, _, calls = _setup(tmp_path, monkeypatch, builds)
    invalidate_quality_entry(entry, "audit failed")

    rows = comparison.quality([CASE])
    assert ("baseline", (0, 1)) in calls
    assert not any(row.get("cached") for row in rows)
    assert sorted(p.name for p in entry.glob("[0-9].json")) == ["0.json"]
