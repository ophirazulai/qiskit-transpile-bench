"""Logs and events: levels, correlation fields, separate files, redaction, tracebacks."""

import json

import pytest

from lsf import job as lsf_job
from lsf import logging as log
from lsf import submit
from lsf.retry import Ledger
from lsf.tests.conftest import FakeScheduler
from lsf.tests.test_manager import launch, manager


def events(path):
    return [json.loads(line) for line in path.open()]


def test_levels_resolve_from_argument_environment_and_default():
    assert log.resolve_level(None, {}) == "INFO"
    assert log.resolve_level(None, {"LSF_LOG_LEVEL": "debug"}) == "DEBUG"
    assert log.resolve_level("warning", {"LSF_LOG_LEVEL": "debug"}) == "WARNING"
    with pytest.raises(ValueError, match="Unknown log level"):
        log.resolve_level("chatty", {})


def test_events_carry_correlation_fields_and_redact_credentials(tmp_path):
    log.setup(tmp_path, "unit", "INFO", "test", console=False)
    try:
        log.bind(run_id="r1", stage="cost", attempt=3, job_id="77")
        log.event("x.happened", "hello", api_token="s3cret", nested={"PASSWORD": "p"}, n=1)
        log.debug("x.hidden", "not at INFO")
        log.flush()
    finally:
        log.close()
    (started, happened) = events(tmp_path / "unit.events.jsonl")
    assert started["event"] == "logging.started"
    assert happened["event"] == "x.happened" and happened["component"] == "test"
    for field in ("ts", "level", "run_id", "stage", "attempt", "job_id", "host", "pid"):
        assert field in happened
    assert happened["api_token"] == "<redacted>" and happened["nested"] == {
        "PASSWORD": "<redacted>"
    }
    readable = (tmp_path / "unit.log").read_text()
    assert "x.happened: hello" in readable and "s3cret" not in readable
    assert "x.hidden" not in readable


def test_files_are_never_shared_and_nested_setups_restore_the_outer_one(tmp_path):
    log.setup(tmp_path, "outer", "INFO", "outer", console=False)
    try:
        log.setup(tmp_path, "outer", "DEBUG", "inner", console=False)  # same name: new files
        log.event("inner.event")
        log.close()
        log.event("outer.event")
    finally:
        log.close()
    files = sorted(p.name for p in tmp_path.glob("outer*.events.jsonl"))
    assert len(files) == 2
    outer = [e["event"] for e in events(tmp_path / "outer.events.jsonl")]
    assert "outer.event" in outer and "inner.event" not in outer


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_manager_logs_show_decisions_at_info_and_every_poll_at_debug(world, tmp_path, level):
    backend = FakeScheduler(pend_polls=3)
    session, lsf_dir = launch(world, tmp_path, backend, "--log-level", level)
    assert manager(lsf_dir, backend).run() == 0
    run_id = Ledger.load(lsf_dir / "ledger.json").data["run_id"]
    logs = lsf_dir / "logs" / run_id
    names = [e["event"] for e in events(logs / "manager-1.events.jsonl")]
    for event in (
        "manager.started",
        "submit.intent",
        "submit.accepted",
        "poll.transition",
        "stage.outcome",
        "retry.decision",
        "decide.verdict",
        "cleanup.complete",
    ):
        assert event in names
    assert ("poll.unchanged" in names) == (level == "DEBUG")
    intent = next(
        e for e in events(logs / "manager-1.events.jsonl") if e["event"] == "submit.intent"
    )
    assert intent["command"].startswith("bsub -J ") and intent["nonce"] and intent["job_key"]
    started = next(
        e for e in events(logs / "manager-1.events.jsonl") if e["event"] == "manager.started"
    )
    assert started["retry_budget"] == {"retries": 20, "jobs": 21}
    assert started["resources"]["cost"]["slots"] == 9
    pending = [
        e for e in events(logs / "manager-1.events.jsonl") if e["event"] == "poll.transition"
    ]
    assert any(e["state"] == "PEND" and e["pending_reason"] for e in pending)
    job_events = events(next((logs / "jobs").glob("job-cost-a01-*.events.jsonl")))
    assert {e.get("attempt") for e in job_events[1:]} == {1}  # all but logging.started
    assert any(e["event"] == "monitor.idle_probe" for e in job_events)
    assert ("monitor.window" in {e["event"] for e in job_events}) is False  # no worker ran
    # The logs are outside the session: its cleanup keeps them.
    assert (session / "clean.json").exists() and (logs / "manager-1.log").exists()


def test_a_failing_job_wrapper_logs_its_traceback_and_reports_why(world, tmp_path, monkeypatch):
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)

    def broken(*args, **kwargs):
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(lsf_job, "run", broken)
    code = lsf_job.main(
        ["--lsf-dir", str(lsf_dir), "--job-key", "quality-x", "--stage", "quality"], environ={}
    )
    assert code == 40
    outcome = lsf_job.outcome_path(lsf_dir, "quality-x")
    assert "adapter exploded" in json.loads(outcome.read_text())["message"]
    run_id = Ledger.load(lsf_dir / "ledger.json").data["run_id"]
    job_events = events(lsf_dir / "logs" / run_id / "jobs" / "job-quality-x.events.jsonl")
    error = next(e for e in job_events if e["event"] == "job.error")
    assert "Traceback" in error["traceback"] and "RuntimeError" in error["traceback"]
    assert submit  # the launcher module is importable next to the job entry point
