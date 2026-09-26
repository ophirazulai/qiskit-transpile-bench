"""The manager against a fake scheduler, with every job run in-process through the real
``lsf.job`` entry point and the harness's stage runner over the fake stage work."""

import json
import signal

import pytest

from qtb.canonical import read_json
from qtb.coordinator import costs
from qtb.coordinator.stages import clean_status, read_state
from qtb.coordinator.storage import locked
from qtb.errors import Contaminated

from lsf import control, lsf_directory, retry, submit
from lsf.manager import Manager
from lsf.retry import Ledger
from lsf.tests.conftest import FakeScheduler


class Clock:
    """Virtual time: sleeping advances it, nothing waits."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class Killed(BaseException):
    """The manager dies without handling anything (SIGKILL, a lost host)."""


def launch(world, tmp_path, backend, *extra, name="s1"):
    session = tmp_path / "sessions" / name
    session.parent.mkdir(exist_ok=True)
    argv = [
        "--store",
        str(world.store),
        "--results-root",
        str(session),
        "--baseline",
        str(world.sources["main"]),
        "--evolved",
        str(world.sources["idea"]),
        "--cost-ncpus",
        "56",
        *extra,
    ]
    assert submit.main(argv, backend=backend, environ={}) == 0
    return session, lsf_directory(session)


def manager(lsf_dir, backend, clock=None, job_id="900"):
    clock = clock or Clock()
    return Manager(
        lsf_dir,
        backend,
        poll_s=60.0,
        summary_s=600.0,
        lock_wait_s=0,
        clock=clock,
        sleep=clock.sleep,
        environ={"LSB_JOBID": job_id},
    )


def noisy(monkeypatch, failures):
    """The first ``failures`` cost measurements detect interference."""
    measured = costs.measure_costs
    calls = []

    def measure(comparison):
        calls.append(1)
        if len(calls) <= failures:
            raise Contaminated("A: foreign CPU 0.300 s > 0.100 s", {"window": {"index": 1}})
        return measured(comparison)

    monkeypatch.setattr("qtb.coordinator.costs.measure_costs", measure)
    return calls


def stages_of(backend):
    return [spec.kind for spec in backend.submitted]


def argv_of(backend, kind):
    return next(spec.argv() for spec in backend.submitted if spec.kind == kind)


def test_the_whole_pipeline_runs_with_fixed_allocations(world, tmp_path):
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend, "--queue", "normal")
    assert manager(lsf_dir, backend).run() == 0
    assert stages_of(backend) == [
        "manager",
        "compile",
        "quality",
        "correctness",
        "unit-tests",
        "cost",
        "clean",
    ]
    for kind in ("manager", "compile", "quality", "correctness", "unit-tests", "clean"):
        argv = argv_of(backend, kind)
        assert argv[argv.index("-n") + 1] == "16" and "-x" not in argv
        assert argv[argv.index("-q") + 1] == "normal"
        assert "affinity[" not in argv[argv.index("-R") + 1]
    cost = argv_of(backend, "cost")
    assert cost[cost.index("-n") + 1] == "9" and "-x" not in cost
    assert cost[cost.index("-R") + 1] == (
        "select[r1m < 10 && ncpus == 56] span[hosts=1] "
        "affinity[core(1,exclusive=(core,alljobs))] rusage[mem=16]"
    )
    assert "-r" in argv_of(backend, "manager")
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert ledger.data["decide"]["status"] == "PASS"
    assert ledger.data["cleanup"]["status"] == "complete"
    assert clean_status(session) == "complete"
    cost_state = read_state(session, "cost")
    assert cost_state["invocation"]["attempt"] == 1 and cost_state["scheduler"]["slots"] == 9
    report = (lsf_dir / "report.md").read_text()
    assert "Verdict: **PASS**" in report and "1 of 21 cost jobs used" in report
    # Orchestration records and logs live outside the session and survive its cleanup.
    logs = lsf_dir / "logs" / ledger.data["run_id"]
    assert (logs / "manager-1.log").exists() and (logs / "launcher.log").exists()
    assert len(list((logs / "jobs").glob("job-*.log"))) == 6


def test_without_unit_tests_the_stage_is_not_submitted(world, tmp_path):
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend, "--no-unit-tests")
    assert manager(lsf_dir, backend).run() == 0
    assert "unit-tests" not in stages_of(backend)


def test_gate_closed_stages_still_run_as_jobs_and_cleanup_follows_any_verdict(world, tmp_path):
    world.knobs.ratio = 1.0
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 10
    assert stages_of(backend)[1:] == [
        "compile",
        "quality",
        "correctness",
        "unit-tests",
        "cost",
        "clean",
    ]
    assert [read_state(session, s)["status"] for s in ("correctness", "unit-tests", "cost")] == [
        "skipped"
    ] * 3
    ledger = Ledger.load(lsf_dir / "ledger.json")
    (attempt,) = ledger.cost_attempts()
    assert attempt["outcome"]["kind"] == "skipped" and attempt["consumes_budget"] is False
    assert retry.budget_used(ledger) == 0
    assert ledger.data["cleanup"]["status"] == "complete"


def test_noisy_cost_is_retried_in_new_jobs_until_it_is_clean(world, tmp_path, monkeypatch):
    noisy(monkeypatch, failures=2)
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 0
    ledger = Ledger.load(lsf_dir / "ledger.json")
    attempts = ledger.cost_attempts()
    assert [(a["attempt"], a["outcome"]["kind"]) for a in attempts] == [
        (1, "noisy"),
        (2, "noisy"),
        (3, "complete"),
    ]
    assert len({a["job_id"] for a in attempts}) == 3
    assert read_state(session, "cost")["invocation"]["job_key"] == attempts[-1]["key"]
    report = read_json(lsf_dir / "report.json")
    assert report["cost"]["used"] == 3 and report["cost"]["remaining"] == 18
    assert "A: foreign CPU" in report["cost"]["attempts"][0]["reason"]


def test_exactly_twenty_retries_then_exhaustion_without_a_false_pass(world, tmp_path, monkeypatch):
    noisy(monkeypatch, failures=10**6)
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 30
    assert stages_of(backend).count("cost") == retry.MAX_COST_ATTEMPTS == 21
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert ledger.data["cost"]["exhausted"]["used"] == 21
    assert read_state(session, "cost")["status"] == "noisy"  # no harness state was edited
    assert ledger.data["decide"]["status"] == "INCONCLUSIVE"
    assert ledger.data["cleanup"]["status"] == "skipped"
    assert "cost (noisy)" in ledger.data["cleanup"]["reason"]
    assert "clean" not in stages_of(backend)
    report = (lsf_dir / "report.md").read_text()
    assert "Retry budget exhausted" in report and "not an observed cost regression" in report
    notes = " ".join(ledger.data["decide"]["notes"])
    assert "no clean measurement" in notes


def test_attempt_counting_survives_a_manager_killed_mid_submission(world, tmp_path, monkeypatch):
    noisy(monkeypatch, failures=10**6)
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    submit_ = backend.submit

    def dying(spec):
        if spec.kind == "cost" and "-a06-" in spec.name:
            raise Killed()  # after the intent was recorded, before bsub answered
        return submit_(spec)

    backend.submit = dying
    with pytest.raises(Killed):
        manager(lsf_dir, backend).run()
    backend.submit = submit_
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert [a["status"] for a in ledger.cost_attempts()][-1] == "reserved"
    # The requeued manager looks for attempt 6 by name for an hour before giving up on it.
    assert manager(lsf_dir, backend).run() == 40
    ledger = Ledger.load(lsf_dir / "ledger.json")
    attempts = ledger.cost_attempts()
    assert len(attempts) == 6 and attempts[5]["status"] == "unreconciled"
    assert attempts[5]["name_lookups"] >= 60 and attempts[5]["outcome"]["kind"] == "lost"
    # It still counts, and without an outcome it allows no retry.
    assert retry.budget_used(ledger) == 6
    assert "ended lost" in ledger.data["cost"]["stopped"]["reason"]
    assert stages_of(backend).count("cost") == 5
    assert len(ledger.data["managers"]) == 2
    assert ledger.data["decide"] is None  # the missing job might still be writing
    logs = lsf_dir / "logs" / ledger.data["run_id"]
    events = [json.loads(line) for line in (logs / "manager-2.events.jsonl").open()]
    assert any(e["event"] == "reconcile.not_found" for e in events)


def test_a_late_answer_to_a_lost_submission_is_adopted_not_duplicated(world, tmp_path):
    backend = FakeScheduler()
    backend.script["cost-a01"] = ["lost"]
    find = backend.find
    calls = []

    def slow_find(name):
        calls.append(name)
        return find(name) if len(calls) > 5 else []  # mbatchd lists it only later

    backend.find = slow_find
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 0
    ledger = Ledger.load(lsf_dir / "ledger.json")
    (attempt,) = ledger.cost_attempts()
    assert attempt["outcome"]["kind"] == "complete" and attempt["name_lookups"] == 5
    assert stages_of(backend).count("cost") == 1


def test_a_killed_last_attempt_consumes_its_budget_and_is_not_retried(world, tmp_path, monkeypatch):
    noisy(monkeypatch, failures=10**6)
    backend = FakeScheduler()
    run_job = backend.run_job

    def runner(spec, environ):
        if spec.kind == "cost" and "-a21-" in spec.name:
            return 137  # LSF killed it before it reported anything
        return run_job(spec, environ)

    backend.runner = runner
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 30
    ledger = Ledger.load(lsf_dir / "ledger.json")
    last = ledger.cost_attempts()[-1]
    assert last["attempt"] == 21 and last["outcome"]["kind"] == "killed"
    assert last["consumes_budget"] is True
    assert stages_of(backend).count("cost") == 21


def test_a_killed_attempt_is_not_a_noise_retry(world, tmp_path):
    backend = FakeScheduler()
    run_job = backend.run_job
    backend.runner = lambda spec, env: 137 if spec.kind == "cost" else run_job(spec, env)
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 30
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert stages_of(backend).count("cost") == 1
    assert "only positive contamination evidence" in ledger.data["cost"]["stopped"]["reason"]


def test_a_lost_submission_response_is_reconciled_by_name(world, tmp_path):
    backend = FakeScheduler()
    backend.script["cost-a01"] = ["lost"]
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 0
    ledger = Ledger.load(lsf_dir / "ledger.json")
    (attempt,) = ledger.cost_attempts()
    assert attempt["job_id"] and attempt["submissions"][0]["outcome"] == "ambiguous"
    assert stages_of(backend).count("cost") == 1


def test_rejected_submissions_are_retried_without_consuming_budget(world, tmp_path):
    backend = FakeScheduler()
    backend.script["cost-a01"] = ["rejected"]
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 0
    ledger = Ledger.load(lsf_dir / "ledger.json")
    (attempt,) = ledger.cost_attempts()
    assert [s["outcome"] for s in attempt["submissions"]] == ["rejected", "accepted"]
    assert retry.budget_used(ledger) == 1
    backend = FakeScheduler()
    backend.script["quality"] = ["rejected"] * 3
    session, lsf_dir = launch(world, tmp_path, backend, name="s2")
    assert manager(lsf_dir, backend).run() == 30
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert ledger.latest("quality") is None  # every quality submission was rejected
    assert "rejected the quality job 3 times" in read_json(lsf_dir / "report.json")["stop_reason"]


def test_queue_waits_count_toward_the_deadline(world, tmp_path):
    backend = FakeScheduler(pend_polls=14 * 60)  # compile pends for 14 virtual hours
    session, lsf_dir = launch(world, tmp_path, backend, "--manager-wall", "20:00")
    assert manager(lsf_dir, backend).run() == 30
    ledger = Ledger.load(lsf_dir / "ledger.json")
    report = read_json(lsf_dir / "report.json")
    # quality (12 h) no longer fits before the manager's internal deadline.
    assert "quality needs up to 12:00" in report["stop_reason"]
    assert ledger.latest("quality") is None
    assert ledger.data["cleanup"]["status"] == "skipped"
    backend = FakeScheduler(pend_polls=10**9)  # never dispatched
    session, lsf_dir = launch(world, tmp_path, backend, "--manager-wall", "20:00", name="s2")
    assert manager(lsf_dir, backend).run() == 40
    (compile_,) = Ledger.load(lsf_dir / "ledger.json").jobs("compile")
    assert compile_["outcome"]["kind"] == "cancelled" and backend.cancelled == [compile_["job_id"]]


def test_a_failed_stage_stops_the_pipeline_but_decide_and_cleanup_still_run(world, tmp_path):
    world.knobs.fail_once.add("quality")
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 40
    assert stages_of(backend)[1:] == ["compile", "quality", "clean"]
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert ledger.latest("quality")["outcome"]["kind"] == "failed"
    assert ledger.data["decide"]["status"] == "ERROR"
    assert ledger.data["cleanup"]["status"] == "complete"


def test_a_compile_that_creates_no_session_is_an_orchestration_failure(world, tmp_path):
    backend = FakeScheduler()
    run_job = backend.run_job
    backend.runner = lambda spec, env: 41 if spec.kind == "compile" else run_job(spec, env)
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 40
    assert not session.exists()
    report = read_json(lsf_dir / "report.json")
    assert report["stop_reason"] == "compile did not create the session"
    assert report["jobs"][0]["outcome"] == "killed"  # it exited without an outcome record


def test_a_second_manager_cannot_schedule_duplicate_work(world, tmp_path):
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    with locked(lsf_dir / "orchestration.lock"):
        assert manager(lsf_dir, backend).run() == 41
    assert stages_of(backend) == ["manager"]


def test_transient_query_failures_are_not_disappearance(world, tmp_path):
    backend = FakeScheduler()
    backend.query_failures = 3
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 0
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert ledger.latest("compile")["query_failures"] == 3


def test_a_job_that_left_bjobs_is_settled_from_its_outcome_record(world, tmp_path):
    backend = FakeScheduler()
    backend.forget_after_run = True
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 0


def test_a_termination_signal_cancels_owned_jobs(world, tmp_path):
    backend = FakeScheduler()
    backend.hold.add("-quality-")
    backend.on_start = lambda spec: (
        signal.raise_signal(signal.SIGTERM) if spec.kind == "quality" else None
    )
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 128 + signal.SIGTERM
    ledger = Ledger.load(lsf_dir / "ledger.json")
    quality = ledger.latest("quality")
    assert quality["outcome"]["kind"] == "cancelled" and backend.cancelled == [quality["job_id"]]
    assert quality["cancel_requested"]["reason"] == "manager received SIGTERM"


def test_a_resumed_manager_adopts_active_jobs_and_reap_recovers_after_a_kill(world, tmp_path):
    backend = FakeScheduler()
    backend.hold.add("-quality-")

    def kill(spec):
        if spec.kind == "quality":
            raise Killed()

    backend.on_start = kill
    session, lsf_dir = launch(world, tmp_path, backend)
    with pytest.raises(Killed):
        manager(lsf_dir, backend).run()
    backend.on_start = None
    quality_id = Ledger.load(lsf_dir / "ledger.json").latest("quality")["job_id"]
    assert backend.jobs[quality_id]["state"] == "RUN"
    # A requeued manager adopts the running job instead of submitting another one.
    backend.hold.clear()
    assert manager(lsf_dir, backend).run() == 0
    assert stages_of(backend).count("quality") == 1
    # After an untrappable kill, reap cancels what the manager left.
    backend = FakeScheduler()
    backend.hold.add("-quality-")
    backend.on_start = kill
    session, lsf_dir = launch(world, tmp_path, backend, name="s2")
    with pytest.raises(Killed):
        manager(lsf_dir, backend).run()
    lines = []
    assert control.main(["reap", "--results-root", str(session)], backend, lines.append) == 0
    quality = Ledger.load(lsf_dir / "ledger.json").latest("quality")
    assert quality["outcome"]["kind"] == "cancelled" and quality["job_id"] in backend.cancelled
    assert control.main(["status", "--results-root", str(session)], backend, lines.append) == 0
    assert any("quality" in line and "cancelled" in line for line in lines)


def test_reap_and_stop_respect_a_running_manager(world, tmp_path):
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    lines = []
    with locked(lsf_dir / "orchestration.lock"):
        assert control.main(["reap", "--results-root", str(session)], backend, lines.append) == 41
    manager_id = Ledger.load(lsf_dir / "ledger.json").latest("manager")["job_id"]
    backend.jobs[manager_id]["state"] = "RUN"
    assert control.main(["stop", "--results-root", str(session)], backend, lines.append) == 0
    assert backend.cancelled == [manager_id]
    other = FakeScheduler(user="someone-else")
    other.jobs = backend.jobs
    backend.jobs[manager_id]["state"] = "RUN"
    assert control.main(["stop", "--results-root", str(session)], other, lines.append) == 41


def test_an_idle_probe_landing_is_retried_and_reported(world, tmp_path):
    from lsf.tests.conftest import SyntheticProbe

    backend = FakeScheduler()
    probes = []

    def factory():
        noisy_host = not probes
        probes.append(noisy_host)
        return SyntheticProbe(foreign=(lambda t: 0.9) if noisy_host else None)

    backend.probe_factory = factory
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 0
    report = read_json(lsf_dir / "report.json")
    first, second = report["cost"]["attempts"]
    assert first["outcome"] == "noisy" and first["phase"] == "idle probe"
    assert first["involuntary_per_s"] is None and first["foreign_fraction"] > 0.5
    assert second["outcome"] == "complete" and second["cpu"] == "Synthetic Xeon"
    assert second["preflight"].endswith("preflight.json")
    assert "idle probe" in (lsf_dir / "report.md").read_text()


def test_a_requeued_manager_reads_the_ledger_only_under_the_lock(world, tmp_path):
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    holder = locked(lsf_dir / "orchestration.lock")
    holder.__enter__()
    clock = Clock()
    m = Manager(lsf_dir, backend, poll_s=60.0, lock_wait_s=600, clock=clock, environ={})

    def sleep(seconds):
        # The old manager records a job, then releases the lock.
        if holder is not None and not getattr(sleep, "released", False):
            ledger = Ledger.load(lsf_dir / "ledger.json")
            ledger.reserve("quality-old", stage="quality", name="qtb-old-quality")
            ledger.update("quality-old", status="terminal", outcome={"kind": "failed"})
            holder.__exit__(None, None, None)
            sleep.released = True
        clock.sleep(seconds)

    m.sleep = sleep
    m.run()
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert "quality-old" in ledger.data["jobs"]
    assert "quality" not in stages_of(backend)  # the recorded job is not duplicated


def test_a_second_signal_does_not_abort_the_cancellation(world, tmp_path):
    backend = FakeScheduler()
    backend.hold.update({"-correctness-", "-unit-tests-"})
    backend.on_start = lambda spec: (
        signal.raise_signal(signal.SIGINT) if spec.kind == "unit-tests" else None
    )
    cancel = backend.cancel

    def cancel_then_sigterm(job_id):
        signal.raise_signal(signal.SIGTERM)  # LSF's next signal, mid-cleanup
        return cancel(job_id)

    backend.cancel = cancel_then_sigterm
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 128 + signal.SIGINT
    ledger = Ledger.load(lsf_dir / "ledger.json")
    for stage in ("correctness", "unit-tests"):
        assert ledger.latest(stage)["outcome"]["kind"] == "cancelled"
    assert len(backend.cancelled) == 2


def test_a_job_whose_identity_cannot_be_checked_is_never_cancelled(world, tmp_path):
    backend = FakeScheduler()
    backend.hold.add("-quality-")

    def kill(spec):
        if spec.kind == "quality":
            raise Killed()

    backend.on_start = kill
    session, lsf_dir = launch(world, tmp_path, backend, name="s3")
    with pytest.raises(Killed):
        manager(lsf_dir, backend).run()
    backend.query_failures = 10**6
    lines = []
    assert control.main(["reap", "--results-root", str(session)], backend, lines.append) == 40
    assert backend.cancelled == []
    assert any("Still unconfirmed" in line for line in lines)


def test_a_requeued_manager_refuses_a_session_of_other_inputs(world, tmp_path):
    from qtb.canonical import write_json

    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 0
    run = read_json(session / "run.json")
    write_json(session / "run.json", dict(run, profile="confirm-profile"))
    before = len(backend.submitted)
    assert manager(lsf_dir, backend).run() == 40
    assert len(backend.submitted) == before


def test_an_unreadable_stage_state_still_yields_an_outcome_record(world, tmp_path):
    from qtb.canonical import write_json

    from lsf import job as lsf_job
    from lsf.tests.conftest import lsf_environment

    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 0
    state = read_json(session / "stages/quality/state.json")
    write_json(session / "stages/quality/state.json", dict(state, format="qtb-stage/9"))
    argv = ["--lsf-dir", str(lsf_dir), "--job-key", "quality-x", "--stage", "quality"]
    assert lsf_job.main(argv, environ=lsf_environment(16)) == 41  # the session is cleaned
    outcome = read_json(lsf_job.outcome_path(lsf_dir, "quality-x"))
    assert outcome["state"]["status"] == "unreadable"


def test_disappearance_with_an_outcome_still_requires_terminal_history(world, tmp_path):
    from qtb.canonical import write_json

    from lsf.job import outcome_path
    from lsf.scheduler import JobStatus

    backend = FakeScheduler()
    _, lsf_dir = launch(world, tmp_path, backend)
    m = manager(lsf_dir, backend)
    m.ledger = Ledger.load(lsf_dir / "ledger.json")
    key = m.submit("quality")
    write_json(outcome_path(lsf_dir, key), {"exit_code": 0})
    backend.query = lambda job_id: JobStatus(job_id, "NOTFOUND")
    backend.history = lambda job_id: None
    assert m.observe(key) is False
    m.cancel_owned("stop", confirm_s=1)
    assert m.ledger.job(key)["status"] == "submitted"
    assert backend.cancelled == []
    assert m.owned()


@pytest.mark.parametrize("missing", ["name", "user"])
def test_missing_identity_fields_do_not_authorize_cancellation(world, tmp_path, missing):
    from lsf.scheduler import JobStatus

    backend = FakeScheduler()
    _, lsf_dir = launch(world, tmp_path, backend)
    m = manager(lsf_dir, backend)
    m.ledger = Ledger.load(lsf_dir / "ledger.json")
    key = m.submit("quality")
    entry = m.ledger.job(key)
    status = JobStatus(entry["job_id"], "RUN", name=entry["name"], user=m.user)
    setattr(status, missing, None)
    backend.query = lambda _: status
    m.cancel_owned("stop", confirm_s=1)
    assert backend.cancelled == []
    assert m.ledger.job(key)["status"] == "submitted"
