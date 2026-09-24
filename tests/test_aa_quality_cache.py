from qtb.canonical import write_json
from qtb.config import case_hash
from qtb.coordinator import Comparison
from qtb.coordinator.storage import cache_quality_observation, quality_cache_key


def test_identical_builds_compile_both_arms_despite_existing_quality_cache(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    fixtures = tmp_path / "fixtures"
    run_dir.mkdir()
    fixtures.mkdir()
    write_json(
        fixtures / "target.json", {"num_qubits": 1, "native_2q_names": [], "instructions": []}
    )
    case = {
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
    build = {"id": "same-build"}
    cache_key = quality_cache_key(build, case, {}, {}, "implementation")
    cache = tmp_path / "quality-cache" / cache_key
    output = tmp_path / "old-output.gz"
    job_file = tmp_path / "old-job.json"
    output.write_bytes(b"old output")
    job_file.write_text("{}")
    cached = {
        "case_id": "aa",
        "case_hash": case_hash(case),
        "build_id": build["id"],
        "revision": "baseline",
        "seed": 0,
        "seed_block": "B0",
        "worker": {"job_file": str(job_file), "output": str(output)},
        "output": str(output),
        "checks": [{"status": "verified"}],
    }
    cache_quality_observation(cache, 0, cached)

    comparison = Comparison.__new__(Comparison)
    comparison.root = tmp_path
    comparison.directory = run_dir
    comparison.fixtures = fixtures
    comparison.run = {
        "builds": {"baseline": build, "evolved": build},
        "machine": {},
        "hashes": {"implementation": "implementation"},
        "profile": "iterations-profile",
    }
    comparison.policy = {"measurement_protocol": {"quality_batch_size": 2}}
    comparison.progress = lambda *_: None
    comparison.routing_batch = lambda *_: {}
    comparison.check_routing = lambda *_: None
    calls = []

    def job(revision, _case, _mode, seeds):
        calls.append((revision, tuple(seeds)))
        path = tmp_path / f"{revision}-output.gz"
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

    rows = comparison.quality([case])
    assert calls == [
        ("baseline", (0, 1)),
        ("baseline", (2,)),
        ("evolved", (0, 1)),
        ("evolved", (2,)),
    ]
    assert [row["revision"] for row in rows] == ["baseline"] * 3 + ["evolved"] * 3
    assert all(not row.get("cached", False) for row in rows)
    assert (cache / "0.json").exists()
