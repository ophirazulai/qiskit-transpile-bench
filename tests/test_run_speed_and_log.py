import threading
import time
from types import SimpleNamespace

import pytest

from qtb.canonical import write_json
from qtb.coordinator import Comparison, cost_stage_due
from qtb.coordinator.checks import clifford_checks
from qtb.coordinator.runlog import RunLog, step
from qtb.evaluator import record


def test_progress_log_records_nested_steps_and_summary(tmp_path):
    path = tmp_path / "progress.log"
    echoed = []
    log = RunLog(path, echoed.append)
    with log.step("Build"):
        log("compiling")
        with log.step("baseline"):
            pass
    with pytest.raises(RuntimeError), log.step("Broken"):
        raise RuntimeError("boom")
    log.summary()

    text = path.read_text()
    assert "=== Started ===" in text
    assert "▶ Build" in text and "✓ Build (" in text
    # Messages and steps inside a step are indented one level.
    assert "  compiling\n" in text and "  ▶ baseline\n" in text
    assert "✗ Broken failed after" in text
    summary = text.split("Step durations", 1)[1]
    assert "Build" in summary and "  baseline" in summary
    assert "(failed)" in summary
    assert "Total (this process)" in summary
    assert "compiling" in echoed

    RunLog(path)
    assert "=== Resumed ===" in path.read_text()


def test_step_helper_is_a_no_op_for_plain_progress():
    comparison = SimpleNamespace(progress=lambda *_: None)
    with step(comparison, "anything"):
        pass
    with step(SimpleNamespace(), "no progress at all"):
        pass


def _quality_comparison(tmp_path, job):
    run_dir = tmp_path / "run"
    fixtures = tmp_path / "fixtures"
    run_dir.mkdir()
    fixtures.mkdir()
    write_json(
        fixtures / "target.json", {"num_qubits": 1, "native_2q_names": [], "instructions": []}
    )
    comparison = Comparison.__new__(Comparison)
    comparison.root = tmp_path
    comparison.directory = run_dir
    comparison.fixtures = fixtures
    comparison.records = []
    comparison.run = {
        "builds": {"baseline": {"id": "a"}, "evolved": {"id": "b"}},
        "machine": {},
        "hashes": {"implementation": "implementation"},
        "profile": "iterations-profile",
    }
    comparison.policy = {"measurement_protocol": {"quality_batch_size": 1}}
    comparison.progress = lambda *_: None
    comparison.routing_batch = lambda *_: {}
    comparison.check_routing = lambda *_: None
    comparison.job = job
    return comparison


def test_quality_batches_run_concurrently_and_commit_in_plan_order(tmp_path, monkeypatch):
    case = {
        "case_id": "c",
        "role": "scored",
        "seeds_per_block": 6,
        "target": {"file": "target.json", "sha256": "target"},
        "circuit": {"sha256": "circuit"},
        "semantic_reference": {"kind": "input"},
        "logical_qubits": 1,
        "options": {},
        "constraint_form": "target",
        "input_domain": "all_zero",
    }
    lock = threading.Lock()
    state = {"running": 0, "peak": 0}

    def job(revision, _case, _mode, seeds):
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        # Later seeds finish first, so completion order is the reverse of plan order.
        time.sleep(0.05 * (6 - seeds[0]))
        with lock:
            state["running"] -= 1
        path = tmp_path / f"{revision}-{seeds[0]}.gz"
        path.write_bytes(b"x")
        job_file = tmp_path / f"{revision}-{seeds[0]}.json"
        job_file.write_text("{}")
        return [
            {"seed": s, "status": "ok", "output": str(path), "output_hash": "h",
             "layout": None, "job_file": str(job_file)}
            for s in seeds
        ]

    comparison = _quality_comparison(tmp_path, job)
    monkeypatch.setattr(
        "qtb.coordinator.structural_result",
        lambda *_: {"status": "verified", "output_hash": "h", "D2": 0, "N2": 0},
    )
    rows = comparison.quality([case])
    assert state["peak"] > 1
    assert [(r["revision"], r["seed"]) for r in rows] == [
        (revision, seed) for revision in ("baseline", "evolved") for seed in range(6)
    ]


@pytest.mark.parametrize(
    "builds, records, expected",
    [
        # Improvement with nothing failed: the usual cost stage.
        ({"baseline": {"id": "a"}, "evolved": {"id": "b"}},
         [record("IA2/improvement", "improvement", "passed")], "improved"),
        # A different build without improvement: no cost stage.
        ({"baseline": {"id": "a"}, "evolved": {"id": "b"}},
         [record("IA2/improvement", "improvement", "failed")], None),
        # A/A: no improvement, but costs are still measured.
        ({"baseline": {"id": "a"}, "evolved": {"id": "a"}},
         [record("IA2/improvement", "improvement", "failed")], "aa"),
        # A/A with a failed correctness check: nothing to measure.
        ({"baseline": {"id": "a"}, "evolved": {"id": "a"}},
         [record("IA2/improvement", "improvement", "failed"),
          record("IA1/C0", "correctness", "failed")], None),
    ],
)
def test_cost_stage_is_forced_for_identical_builds(builds, records, expected):
    assert cost_stage_due(builds, records) == expected


@pytest.mark.parametrize("prefix, full", [("IA", False), ("CA", True)])
def test_full_pipeline_clifford_checks_run_only_in_confirm_profile(tmp_path, prefix, full):
    specs_seen = []

    def jobs(specs):
        specs = list(specs)
        specs_seen.extend(specs)
        return [[{"status": "ok", "seed": seed} for seed in spec[3]] for spec in specs]

    comparison = SimpleNamespace(
        directory=tmp_path,
        prefix=prefix,
        jobs=jobs,
        verify_many=lambda requests: [{"status": "verified"} for _ in requests],
        evidence=lambda _row: None,
    )
    case = {"case_id": "sample", "clifford_variant": {"sha256": "fixture"}, "options": {}}
    clifford_checks(comparison, [case])
    edits = [spec[4] for spec in specs_seen]
    assert ([] in edits) is full
    assert all(e == [] or "drop_stage:optimization" in e for e in edits)
    modes = {
        line.split('"mode":')[1].split('"')[1]
        for line in (tmp_path / "clifford.jsonl").read_text().splitlines()
    }
    assert modes == ({"full", "prefix"} if full else {"prefix"})
