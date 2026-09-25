import shutil
import sys

from qtb.canonical import read_json
from qtb.config import data_root
from qtb.coordinator import Comparison


def _comparison(tmp_path, stage="quality"):
    comparison = object.__new__(Comparison)
    comparison.directory = tmp_path / "session"
    comparison.directory.mkdir(parents=True, exist_ok=True)
    comparison.stage = stage
    comparison.data = data_root()
    comparison.fixtures = comparison.data / "fixtures"
    # The harness venv pins the verifier's Qiskit 2.5.2, so it can host the verifier.
    comparison.run = {"hashes": {"implementation": "test"}, "verifier_python": sys.executable}
    return comparison


def _request(comparison, tmp_path, name, oracle="C1"):
    suite = read_json(comparison.fixtures / "correctness-suite.json")
    case = next(c for c in suite["cases"] if c["circuit"]["file"] == "circuits/c1_n2.ops.jsonl.gz")
    # The input itself, left in place, is a correct compilation of itself.
    output = tmp_path / name
    shutil.copy(comparison.fixtures / case["circuit"]["file"], output)
    return case, {"output": str(output), "layout": None}, oracle, None, {}


def _jobs(comparison):
    return [p for p in (comparison.directory / "oracle-jobs").iterdir() if len(p.name) == 32]


def test_identical_outputs_are_verified_once_and_cached_across_stages(tmp_path):
    comparison = _comparison(tmp_path)
    first = _request(comparison, tmp_path, "seed0.ops.jsonl.gz")
    second = _request(comparison, tmp_path, "seed1.ops.jsonl.gz")

    results = comparison.verify_many([first, second])

    assert [r["status"] for r in results] == ["verified", "verified"]
    assert len(_jobs(comparison)) == 1
    results[0]["mutated"] = True
    assert "mutated" not in results[1]

    # A later stage of the same session reuses the session's verifier cache.
    jobs_before = len(_jobs(comparison))
    later = _comparison(tmp_path, "correctness")
    again = later.verify_many([first])
    assert again[0]["status"] == "verified" and again[0]["cached"]
    assert len(_jobs(later)) == jobs_before
    # A damaged or indecisive cache entry is never reused.
    [entry] = (later.directory / "verifier-cache").rglob("*.json")
    entry.write_text('{"status": "unverified"}\n')
    assert "cached" not in later.verify_many([first])[0]


def test_a_stopped_batch_marks_only_its_current_job_unverified(tmp_path, monkeypatch):
    comparison = _comparison(tmp_path)
    monkeypatch.setattr(Comparison, "workers", property(lambda self: 1))
    requests = [
        _request(comparison, tmp_path, "a.ops.jsonl.gz", oracle)
        for oracle in ("C1", "C2", "C1-lite")
    ]
    calls = []

    def stop_after_first_call(python, pairs, directory, idle_timeout=300):
        calls.append(len(pairs))
        if len(calls) == 1:
            return "Verifier exited -9"
        from qtb.canonical import write_json

        for pair in pairs:
            write_json(pair["out"], {"status": "verified"})
        return None

    monkeypatch.setattr("qtb.coordinator.process.run_verifier_batch", stop_after_first_call)

    results = comparison.verify_many(requests)

    assert calls == [3, 2]
    assert results[0] == {"status": "unverified", "oracle": "C1", "detail": "Verifier exited -9"}
    assert [r["status"] for r in results[1:]] == ["verified", "verified"]
    cache = comparison.directory / "verifier-cache"
    assert len(list(cache.rglob("*.json"))) == 2
