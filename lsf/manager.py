"""The manager job: one session's whole pipeline on LSF. ``python -P -m lsf.manager``.

The launcher submits it once, with 16 slots. It holds the session's orchestration lock for
its lifetime and never runs a stage body itself::

    compile -> quality -> correctness + unit-tests (in parallel) -> cost (noise retries)
            -> decide (inline) -> clean (its own job, when the session is safe to clean)

Each stage is its own job with its configured allocation; a stage whose gate is closed runs
and records ``skipped`` itself. A stage already ``complete`` or ``skipped`` is not
submitted again, and a stage job that already ended is never resubmitted: only a cost job
that ended ``noisy`` leads to another cost job, within the ledger's budget (``lsf.retry``).

A stage outcome is trusted only when the scheduler's terminal state, the job's outcome
record and the committed stage state agree on the invocation. Pending time counts toward
the manager's internal deadline, which ends before its wall limit; a job that cannot fit
the remaining time is not submitted. On a termination signal or at the deadline the
manager cancels the jobs it owns, after checking their ID, name and user, and waits for
confirmation. After a kill it cannot trap, ``python -m lsf.control reap`` does the same.

If the manager is requeued, the new instance reconciles the ledger first: it adopts jobs
that are still active and settles those that ended. The exit status is the verdict's.
"""

import argparse
import getpass
import os
import signal
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from qtb.canonical import read_json
from qtb.config import implementation_identity
from qtb.coordinator.stages import FINAL, clean_status, read_state
from qtb.coordinator.storage import LockBusy, locked
from qtb.errors import HarnessError

from lsf import logging as log
from lsf import report, retry
from lsf.job import log_directory, outcome_path
from lsf.retry import ACTIVE, Ledger
from lsf.scheduler import JobSpec, LsfBackend, QueryFailed, wall_seconds

EXIT_ERROR, EXIT_PRECONDITION = 40, 41
DEADLINE_SLACK_S = 15 * 60
LOCK_WAIT_S = 600
LOST_AFTER_S = 3600
NAME_LOOKUPS = 3
CANCEL_CONFIRM_S = 60
SIGNAL_CONFIRM_S = 8
SIGNALS = ("SIGTERM", "SIGINT", "SIGHUP", "SIGUSR2", "SIGXCPU")


class Cancelled(BaseException):
    def __init__(self, signum):
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


class DeadlineReached(Exception):
    pass


class Manager:
    def __init__(
        self,
        lsf_dir,
        backend=None,
        *,
        poll_s=30.0,
        summary_s=600.0,
        lock_wait_s=LOCK_WAIT_S,
        lost_after_s=LOST_AFTER_S,
        clock=time.monotonic,
        sleep=time.sleep,
        environ=None,
    ):
        self.lsf_dir = Path(lsf_dir).resolve()
        self.launch = read_json(self.lsf_dir / "launch.json")
        self.session = Path(self.launch["session"])
        self.run_id = self.launch["run_id"]
        self.log_dir = log_directory(self.lsf_dir, self.run_id)
        self.backend = backend or LsfBackend(diagnostics=self.log_dir / "diagnostics")
        self.poll_s, self.summary_s = poll_s, summary_s
        self.lock_wait_s, self.lost_after_s = lock_wait_s, lost_after_s
        self.clock, self.sleep = clock, sleep
        self.environ = os.environ if environ is None else environ
        self.user = getpass.getuser()
        self.started = clock()
        wall = wall_seconds(self.launch["resources"]["manager"]["wall"])
        self.deadline = self.started + wall - min(DEADLINE_SLACK_S, wall / 4)
        self.ledger = None
        self.stop_reason = None
        self._seen = {}

    # Entry points

    def run(self):
        """The whole pipeline; returns the verdict's exit status."""
        try:
            with self._lock():
                # Read under the lock: an earlier holder may have changed it while we waited.
                self.ledger = Ledger.load(self.lsf_dir / "ledger.json")
                index = len(self.ledger.data["managers"]) + 1
                log.setup(self.log_dir, f"manager-{index}", self.launch["log_level"], "manager")
                log.bind(
                    run_id=self.run_id,
                    session=str(self.session),
                    job_id=self.environ.get("LSB_JOBID"),
                )
                try:
                    return self._run(index)
                finally:
                    log.flush()
                    log.close()
        except LockBusy as exc:
            print(f"ERROR: another manager holds the session: {exc}", file=sys.stderr)
            return EXIT_PRECONDITION

    def reap(self):
        """Manual recovery after a manager died: cancel and settle every owned job."""
        log.setup(self.log_dir, "reap", self.launch["log_level"], "control")
        log.bind(run_id=self.run_id, session=str(self.session))
        try:
            with locked(self.lsf_dir / "orchestration.lock", wait=False):
                self.ledger = Ledger.load(self.lsf_dir / "ledger.json")
                self.reconcile()
                cancelled = self.cancel_owned("manual reap", CANCEL_CONFIRM_S, manager=True)
                report.write(self)
                return cancelled
        finally:
            log.close()

    @contextmanager
    def _lock(self):
        """The session's orchestration lock; a requeued manager waits for a stale holder."""
        path = self.lsf_dir / "orchestration.lock"
        waited = self.clock()
        while True:
            lock = locked(path, wait=False)
            try:
                lock.__enter__()
            except LockBusy:
                if self.clock() - waited >= self.lock_wait_s:
                    raise
                print(f"Waiting for the orchestration lock {path}", file=sys.stderr)
                self.sleep(min(10.0, max(self.poll_s, 0.01)))
                continue
            try:
                yield
            finally:
                lock.__exit__(None, None, None)
            return

    def _run(self, index):
        wall_deadline = datetime.now(UTC) + timedelta(seconds=self.deadline - self.clock())
        self.ledger.data["managers"].append(
            {
                "index": index,
                "job_id": self.environ.get("LSB_JOBID"),
                "host": log.bound().get("host"),
                "pid": os.getpid(),
                "started_at": retry.now(),
                "deadline_at": wall_deadline.isoformat(),
                "logs": {k: str(v) for k, v in log.paths().items()},
            }
        )
        self.ledger.save()
        resources = self.launch["resources"]
        log.event(
            "manager.started",
            f"manager {index} for {self.session}",
            resources=resources,
            cost_selector=self.launch["cost_selector"],
            tier=self.launch["tier"],
            unit_tests=self.launch["unit_tests"],
            retry_budget={"retries": retry.MAX_COST_RETRIES, "jobs": retry.MAX_COST_ATTEMPTS},
            deadline_at=wall_deadline.isoformat(),
            measurement_identity=self.launch.get("measurement_identity"),
            implementation_identity=implementation_identity(),
            session_path=str(self.session),
            ledger=str(self.ledger.path),
        )
        previous = {name: signal.signal(getattr(signal, name), self._signal) for name in SIGNALS}
        code = EXIT_ERROR
        try:
            self.reconcile()
            self.check_session()
            code = self.pipeline()
        except Cancelled as exc:
            # LSF follows SIGINT with SIGTERM: a second signal must not abort the cleanup.
            self._ignore_signals()
            log.warning("manager.cancelled", f"{exc.args[0]}: cancelling owned jobs")
            log.flush()
            self.cancel_owned(f"manager received {exc.args[0]}", SIGNAL_CONFIRM_S)
            code = 128 + exc.signum
        except Exception as exc:
            self._ignore_signals()
            log.error("manager.error", f"{type(exc).__name__}: {exc}", exc_info=True)
            self.cancel_owned("manager error", CANCEL_CONFIRM_S)
        finally:
            for name, handler in previous.items():
                signal.signal(getattr(signal, name), handler)
            self.ledger.data["managers"][-1].update(ended_at=retry.now(), exit_code=code)
            self.ledger.data["result"] = {"exit_code": code, "at": retry.now()}
            self.ledger.save()
            report.write(self)
            log.event("manager.finished", f"exit {code}", exit_code=code)
        return code

    def _signal(self, signum, _frame):
        raise Cancelled(signum)

    def _ignore_signals(self):
        for name in SIGNALS:
            signal.signal(getattr(signal, name), signal.SIG_IGN)

    def check_session(self):
        """A resumed pipeline continues only with the session this launch created."""
        path = self.session / "run.json"
        if not path.exists():
            return
        run = read_json(path)
        wanted = {
            "sources": {"baseline": self.launch["baseline"], "evolved": self.launch["evolved"]},
            "profile": self.launch["profile"],
            "store": self.launch["store"],
        }
        differ = [key for key, value in wanted.items() if run.get(key) != value]
        if differ:
            raise HarnessError(
                f"{self.session} holds another comparison ({', '.join(differ)} differ from "
                "launch.json); the manager will not continue it"
            )

    # The pipeline

    def remaining(self):
        return self.deadline - self.clock()

    def state(self, stage):
        if not (self.session / "run.json").exists():
            return None
        return read_state(self.session, stage)

    def final(self, stage):
        state = self.state(stage)
        return bool(state) and state["status"] in FINAL

    def pipeline(self):
        try:
            self.stage_jobs(["compile"])
            if not (self.session / "run.json").exists():
                self.stop_reason = "compile did not create the session"
                log.error("manager.orchestration_failed", self.stop_reason)
                return EXIT_ERROR
            if self.final("compile"):
                self.stage_jobs(["quality"])
            if self.final("quality"):
                parallel = ["correctness"] + (["unit-tests"] if self.launch["unit_tests"] else [])
                self.stage_jobs(parallel)
            if self.final("correctness"):
                self.cost_loop()
        except DeadlineReached as exc:
            self.stop_reason = str(exc)
            log.warning("deadline.reached", str(exc), remaining_s=self.remaining())
            self.cancel_owned("deadline", CANCEL_CONFIRM_S)
            if not (self.session / "run.json").exists():
                return EXIT_ERROR
        if self.owned():
            self.stop_reason = "child termination is unconfirmed; run lsf.control reap"
            log.error("manager.unsettled", self.stop_reason)
            return EXIT_ERROR
        return self.decide()

    def stage_jobs(self, stages):
        keys = []
        for stage in stages:
            latest = self.ledger.latest(stage)
            if latest and latest["status"] in ACTIVE:
                log.event("stage.adopted", f"waiting for {latest['key']}", stage=stage)
                keys.append(latest["key"])
                continue
            state = self.state(stage)
            if state and state["status"] in FINAL:
                log.event("stage.omitted", f"{stage} is already {state['status']}", stage=stage)
                continue
            if latest:
                log.event(
                    "stage.not_resubmitted",
                    f"{stage} job {latest['key']} already ended "
                    f"{(latest.get('outcome') or {}).get('kind')}",
                    stage=stage,
                )
            else:
                keys.append(self.submit(stage))
        self.wait([key for key in keys if key])
        if any(e["status"] == "unreconciled" for e in self.owned()):
            raise HarnessError("child termination is unconfirmed; run lsf.control reap")

    def cost_loop(self):
        while True:
            latest = self.ledger.latest("cost")
            if latest and latest["status"] in ACTIVE:
                self.wait([latest["key"]])
                continue
            state = self.state("cost")
            if state and state["status"] in FINAL:
                log.event("cost.final", f"cost is {state['status']}", stage="cost")
                return
            decision = retry.decision(self.ledger)
            log.event("retry.decision", decision["reason"], stage="cost", **decision)
            if not decision["allowed"]:
                if decision["exhausted"]:
                    retry.record_exhaustion(self.ledger, decision["reason"])
                else:
                    retry.record_stop(self.ledger, decision["reason"])
                return
            key = self.submit("cost", attempt=decision["next_attempt"])
            if key is None:
                retry.record_stop(self.ledger, self.stop_reason)
                return
            self.wait([key])

    def decide(self):
        from qtb.coordinator.clean import after_decide
        from qtb.coordinator.decide import decide

        log.event("decide.started", f"deciding {self.session}")
        try:
            code, decision = decide(
                self.session, progress=lambda line: log.event("decide.progress", line)
            )
        except Exception as exc:
            log.error("decide.error", f"{type(exc).__name__}: {exc}", exc_info=True)
            self.ledger.data["decide"] = {"at": retry.now(), "error": str(exc)}
            self.ledger.save()
            return EXIT_ERROR
        self.ledger.data["decide"] = {
            "at": retry.now(),
            "status": decision["status"],
            "exit_code": code,
            "notes": decision.get("notes", []),
        }
        self.ledger.save()
        log.event("decide.verdict", f"{decision['status']} (exit {code})", exit_code=code)
        cleanup = after_decide(
            self.session,
            decision,
            progress=lambda line: log.event("cleanup.progress", line),
            cleanup=self.cleanup_job,
        )
        self.ledger.data["cleanup"] = dict(cleanup, at=retry.now())
        self.ledger.save()
        level = "INFO" if cleanup["status"] in {"complete", "skipped"} else "WARNING"
        log.event(
            f"cleanup.{cleanup['status']}",
            cleanup.get("reason") or "cleanup finished",
            level=level,
            **cleanup,
        )
        return code

    def cleanup_job(self, root, progress):
        """``decide``'s follow-up on LSF: ``clean`` as its own 16-slot job, submitted once."""
        latest = self.ledger.latest("clean")
        if latest and latest["status"] not in ACTIVE:
            key = latest["key"]
            log.event("cleanup.once", f"cleanup job {key} already ran")
        else:
            key = latest["key"] if latest else self.submit("clean")
            if key is None:
                return {"status": "skipped", "reason": self.stop_reason}
            try:
                self.wait([key])
            except DeadlineReached as exc:
                self.cancel_owned("deadline", CANCEL_CONFIRM_S)
                return {"status": "failed", "reason": str(exc), "job_key": key}
        outcome = self.ledger.job(key).get("outcome") or {}
        if outcome.get("kind") == "complete" or clean_status(root) == "complete":
            return {"status": "complete", "job_key": key}
        if outcome.get("kind") == "precondition":
            return {"status": "skipped", "reason": outcome.get("message"), "job_key": key}
        return {
            "status": "failed",
            "reason": f"clean job ended {outcome.get('kind')}: {outcome.get('message')}",
            "job_key": key,
        }

    # Submitting

    def spec(self, stage, key, attempt):
        resources = self.launch["resources"][stage]
        command = [
            self.launch["python"],
            "-P",
            "-m",
            "lsf.job",
            "--lsf-dir",
            str(self.lsf_dir),
            "--job-key",
            key,
            "--stage",
            stage,
            "--log-level",
            self.launch["log_level"],
        ]
        if attempt is not None:
            command += ["--attempt", str(attempt)]
        return JobSpec(
            kind=stage,
            name=f"qtb-{self.run_id}-{key}",
            command=command,
            queue=resources["queue"],
            mem_gb=resources["mem_gb"],
            wall=resources["wall"],
            output=str(self.log_dir / "jobs" / f"{key}.%J.out"),
            selector=self.launch["cost_selector"] if stage == "cost" else {},
        )

    def submit(self, stage, attempt=None):
        """Reserve, then submit; returns the job key, or ``None`` when nothing was accepted."""
        nonce = uuid.uuid4().hex[:8]
        key = f"{stage}-a{attempt:02d}-{nonce}" if attempt else f"{stage}-{nonce}"
        spec = self.spec(stage, key, attempt)
        needed = wall_seconds(spec.wall)
        if self.remaining() < needed:
            self.stop_reason = (
                f"{stage} needs up to {spec.wall} but only "
                f"{max(0, self.remaining()) / 3600:.1f} h remain before the manager's deadline"
            )
            log.warning("deadline.no_fit", self.stop_reason, stage=stage, attempt=attempt)
            return None
        argv = spec.argv()
        self.ledger.reserve(
            key,
            stage=stage,
            attempt=attempt,
            name=spec.name,
            nonce=nonce,
            command=argv,
            resources={
                "slots": argv[argv.index("-n") + 1],
                "queue": spec.queue,
                "mem_gb": spec.mem_gb,
                "wall": spec.wall,
                "request": argv[argv.index("-R") + 1],
            },
            output=spec.output,
        )
        log.event(
            "submit.intent",
            f"{stage} as {spec.name}",
            stage=stage,
            attempt=attempt,
            job_key=key,
            nonce=nonce,
            job_name=spec.name,
            command=log.quote(argv),
            argv=argv,
        )
        log.flush()
        for _ in range(retry.MAX_REJECTIONS):
            result = self.backend.submit(spec)
            entry = self.ledger.job(key)
            entry["submissions"].append(
                {
                    "at": retry.now(),
                    "outcome": result.outcome,
                    "job_id": result.job_id,
                    "returncode": result.returncode,
                    "seconds": result.seconds,
                    "response": (result.stdout + result.stderr).strip()[:2000],
                }
            )
            level = "INFO" if result.outcome == "accepted" else "WARNING"
            log.event(
                f"submit.{result.outcome}",
                (result.stdout + result.stderr).strip()[:500] or result.outcome,
                level=level,
                stage=stage,
                attempt=attempt,
                job_key=key,
                job_id=result.job_id,
                returncode=result.returncode,
                seconds=result.seconds,
            )
            if result.outcome == "accepted":
                self.ledger.update(key, status="submitted", job_id=result.job_id)
                self._seen[key] = {"state": None, "since": self.clock()}
                return key
            if result.outcome == "ambiguous":
                self.ledger.update(key, status="ambiguous", name_lookups=0)
                return key
            self.ledger.save()
            self.sleep(min(self.poll_s, 30.0))
        self.ledger.update(key, status="rejected")
        self.stop_reason = f"LSF rejected the {stage} job {retry.MAX_REJECTIONS} times"
        log.error("submit.gave_up", self.stop_reason, stage=stage, job_key=key)
        return None

    # Waiting

    def wait(self, keys):
        pending = list(keys)
        last_summary = self.clock()
        while pending:
            for key in list(pending):
                if self.observe(key):
                    pending.remove(key)
            if not pending:
                return
            if self.clock() >= self.deadline:
                raise DeadlineReached(
                    "the manager's deadline passed while waiting for " + ", ".join(pending)
                )
            if self.clock() - last_summary >= self.summary_s:
                last_summary = self.clock()
                for key in pending:
                    self._summary(key)
            self.sleep(self.poll_s)

    def _summary(self, key):
        entry, seen = self.ledger.job(key), self._seen.get(key, {})
        log.event(
            "poll.waiting",
            f"{entry['stage']} {seen.get('state') or entry['status']} for "
            f"{(self.clock() - seen.get('since', self.clock())) / 60:.0f} min",
            stage=entry["stage"],
            attempt=entry.get("attempt"),
            job_key=key,
            job_id=entry.get("job_id"),
            pending_reason=seen.get("pending_reason"),
            remaining_deadline_s=self.remaining(),
        )

    def observe(self, key):
        """Poll one job; settles it and returns ``True`` once it is terminal."""
        entry = self.ledger.job(key)
        if entry["status"] in {"reserved", "ambiguous"}:
            self.reconcile_by_name(entry)
            entry = self.ledger.job(key)
            if entry["status"] == "unreconciled":
                self.settle(key, None)
                return True
            if entry["status"] != "submitted":
                return False
        if entry["status"] not in ACTIVE:
            return True
        try:
            status = self.backend.query(entry["job_id"])
        except QueryFailed as exc:
            failures = entry.get("query_failures", 0) + 1
            self.ledger.update(key, query_failures=failures)
            log.warning(
                "poll.query_failed",
                f"bjobs failed ({failures} so far): {exc}; the job is not assumed gone",
                job_key=key,
                job_id=entry["job_id"],
            )
            return False
        self._track(key, entry, status)
        if status.state == "NOTFOUND":
            history = self.backend.history(entry["job_id"])
            if history is not None:
                self.settle(key, history)
                return True
            lost_for = self.clock() - self._seen[key].setdefault("missing_since", self.clock())
            if lost_for >= self.lost_after_s:
                log.error(
                    "poll.lost",
                    f"job {entry['job_id']} is unknown to bjobs and bhist for "
                    f"{lost_for / 60:.0f} min; termination cannot be confirmed",
                    job_key=key,
                    job_id=entry["job_id"],
                )
                self.ledger.update(key, status="unreconciled")
                self.settle(key, None, lost=True)
                return True
            return False
        self._seen[key].pop("missing_since", None)
        if status.terminal:
            self.settle(key, status)
            return True
        return False

    def _track(self, key, entry, status):
        seen = self._seen.setdefault(key, {"state": None, "since": self.clock()})
        fields = dict(
            stage=entry["stage"],
            attempt=entry.get("attempt"),
            job_key=key,
            job_id=entry["job_id"],
            state=status.state,
            host=status.host,
            pending_reason=status.pending_reason,
        )
        if status.state != seen["state"]:
            elapsed = self.clock() - seen["since"]
            log.event(
                "poll.transition",
                f"{seen['state'] or 'submitted'} -> {status.state} after {elapsed:.0f} s",
                elapsed_s=elapsed,
                remaining_deadline_s=self.remaining(),
                **fields,
            )
            seen.update(
                state=status.state, since=self.clock(), pending_reason=status.pending_reason
            )
        else:
            log.debug("poll.unchanged", status.state, **fields)

    def reconcile_by_name(self, entry):
        """An unanswered submission is looked up by its unique name before anything else."""
        try:
            found = self.backend.find(entry["name"])
        except QueryFailed as exc:
            log.warning("reconcile.query_failed", str(exc), job_key=entry["key"])
            return
        mine = [s for s in found if s.user == self.user and s.name == entry["name"]]
        if mine:
            job_id = mine[0].job_id
            self.ledger.update(entry["key"], status="submitted", job_id=job_id)
            self._seen[entry["key"]] = {"state": None, "since": self.clock()}
            log.event(
                "reconcile.found",
                f"{entry['name']} is job {job_id}",
                job_key=entry["key"],
                job_id=job_id,
            )
            for extra in mine[1:]:
                log.error("reconcile.duplicate", f"cancelling duplicate {extra.job_id}")
                self.backend.cancel(extra.job_id)
            return
        lookups = entry.get("name_lookups", 0) + 1
        since = self._seen.setdefault(entry["key"], {"state": None, "since": self.clock()})
        waited = self.clock() - since.setdefault("lookup_since", self.clock())
        # Given up only after a long silence; the entry still counts and is still looked up.
        give_up = lookups >= NAME_LOOKUPS and waited >= self.lost_after_s
        status = "unreconciled" if give_up or entry["status"] == "unreconciled" else entry["status"]
        self.ledger.update(entry["key"], name_lookups=lookups, status=status)
        log.warning(
            "reconcile.not_found",
            f"no job named {entry['name']} ({lookups} lookups)"
            + ("; it still counts as an attempt" if status == "unreconciled" else ""),
            job_key=entry["key"],
        )

    def owned(self, manager=False):
        """Jobs this session may still have in LSF; the manager's own entry only when asked.

        An unreconciled submission is included: its job may yet appear under its name.
        """
        return [
            e
            for e in self.ledger.jobs()
            if (e["status"] in ACTIVE or e["status"] == "unreconciled")
            and (manager or e["stage"] != "manager")
        ]

    def reconcile(self):
        """Settle or adopt every job an earlier manager left unfinished."""
        for entry in self.owned():
            if entry["status"] in {"reserved", "ambiguous", "unreconciled"}:
                self.reconcile_by_name(entry)
                continue
            try:
                status = self.backend.query(entry["job_id"])
            except QueryFailed as exc:
                log.warning("reconcile.query_failed", str(exc), job_key=entry["key"])
                continue
            self._seen[entry["key"]] = {"state": status.state, "since": self.clock()}
            if status.state == "NOTFOUND":
                status = self.backend.history(entry["job_id"]) or status
            if status.terminal:
                self.settle(entry["key"], status)
            else:
                log.event(
                    "reconcile.adopted",
                    f"{entry['stage']} job {entry['job_id']} is {status.state}",
                    stage=entry["stage"],
                    job_key=entry["key"],
                    job_id=entry["job_id"],
                )

    # Outcomes

    def settle(self, key, status, lost=False, cancelled=None):
        entry = self.ledger.job(key)
        path = outcome_path(self.lsf_dir, key)
        record = read_json(path) if path.exists() else None
        kind = self.classify(entry, status, record, lost)
        if cancelled and kind in {"killed", "terminated", "lost", "unknown"}:
            kind = "cancelled"
        state = (record or {}).get("state") or {}
        outcome = {
            "kind": kind,
            "exit_code": (record or {}).get("exit_code"),
            "message": (record or {}).get("message"),
            "state": state.get("status"),
            "reason": state.get("reason") or cancelled,
            "invocation": state.get("invocation"),
            "contamination": state.get("contamination"),
            "monitor": (record or {}).get("monitor"),
            "host": (record or {}).get("host") or getattr(status, "host", None),
            "logs": (record or {}).get("logs"),
        }
        scheduler = {
            "state": getattr(status, "state", None),
            "exit_code": getattr(status, "exit_code", None),
        }
        self.ledger.update(
            key,
            status="unreconciled" if entry["status"] == "unreconciled" else "terminal",
            scheduler=scheduler,
            outcome=outcome,
            settled_at=retry.now(),
            consumes_budget=kind not in {"skipped", "already-final"},
        )
        if (
            record
            and status
            and status.terminal
            and (status.state == "DONE") != (record["exit_code"] == 0)
        ):
            log.warning(
                "stage.mismatch",
                f"LSF says {status.state} ({status.exit_code}), the job recorded exit "
                f"{record['exit_code']}; the job's record is used",
                job_key=key,
            )
        level = "INFO" if kind in {"complete", "skipped", "noisy", "already-final"} else "WARNING"
        log.event(
            "stage.outcome",
            f"{entry['stage']} job {entry.get('job_id')} ended {kind}"
            + (f": {outcome['reason']}" if outcome["reason"] else ""),
            level=level,
            stage=entry["stage"],
            attempt=entry.get("attempt"),
            job_key=key,
            job_id=entry.get("job_id"),
            scheduler=scheduler,
            outcome=outcome,
        )
        report.write(self)

    def classify(self, entry, status, record, lost=False):
        """The stage outcome; only a verified contamination is ``noisy``."""
        if lost or entry["status"] == "unreconciled":
            return "lost"
        if record is None:
            if status is not None and status.state == "EXIT":
                return "killed"
            return "unknown"
        code, stage, key = record["exit_code"], entry["stage"], entry["key"]
        if stage == "manager":
            return "complete" if code == 0 else "failed"
        if stage == "clean":
            return {0: "complete", 41: "precondition"}.get(code, "failed")
        state = self.state(stage) or {}
        mine = (state.get("invocation") or {}).get("job_key") == key
        if code == 0:
            if state.get("status") in FINAL:
                return state["status"] if mine else "already-final"
            return "inconsistent"
        if code == 42:
            return "noisy" if state.get("status") == "noisy" and mine else "inconsistent"
        if code >= 128:
            return "terminated"
        return {40: "failed", 41: "precondition", 64: "usage"}.get(code, "failed")

    # Cancelling

    def cancel_owned(self, reason, confirm_s, manager=False):
        """Cancel every active job this session owns, then wait for confirmation."""
        targets = []
        for entry in self.owned(manager):
            if entry["status"] in {"reserved", "ambiguous", "unreconciled"}:
                self.reconcile_by_name(entry)
                entry = self.ledger.job(entry["key"])
                if entry["status"] != "submitted":
                    continue
            try:
                status = self.backend.query(entry["job_id"])
            except QueryFailed as exc:
                # Its identity cannot be checked, so it is not cancelled; reap retries it.
                log.warning(
                    "cancel.unverified",
                    f"not cancelled, identity unverifiable: {exc}",
                    job_key=entry["key"],
                    job_id=entry["job_id"],
                )
                continue
            if status.state == "NOTFOUND":
                status = self.backend.history(entry["job_id"]) or status
            if status.terminal:
                self.settle(entry["key"], status, cancelled=reason)
                continue
            if status.name != entry["name"] or status.user != self.user:
                log.error(
                    "cancel.identity_mismatch",
                    f"job {entry['job_id']} is {status.name} of {status.user}; not cancelled",
                    job_key=entry["key"],
                )
                continue
            ok, response = self.backend.cancel(entry["job_id"])
            log.event(
                "cancel.requested",
                f"bkill {entry['job_id']}: {response}",
                level="INFO" if ok else "WARNING",
                reason=reason,
                job_key=entry["key"],
                job_id=entry["job_id"],
            )
            self.ledger.update(entry["key"], cancel_requested={"at": retry.now(), "reason": reason})
            targets.append(entry["key"])
        log.flush()
        started = self.clock()
        pending = list(targets)
        while pending and self.clock() - started < confirm_s:
            for key in list(pending):
                try:
                    status = self.backend.query(self.ledger.job(key)["job_id"])
                except QueryFailed:
                    continue
                if status.state == "NOTFOUND":
                    status = self.backend.history(self.ledger.job(key)["job_id"]) or status
                if status.terminal:
                    self.settle(key, status, cancelled=reason)
                    pending.remove(key)
            if pending:
                self.sleep(min(1.0, max(self.poll_s, 0.01)))
        unconfirmed = [entry["key"] for entry in self.owned(manager)]
        if unconfirmed:
            log.error(
                "cancel.unconfirmed",
                "termination not confirmed; run: python -m lsf.control reap "
                f"--results-root {self.session}",
                jobs=unconfirmed,
            )
        else:
            log.event("cancel.confirmed", f"{len(targets)} jobs ended", jobs=targets)
        log.flush()
        return targets


def main(argv=None):
    cli = argparse.ArgumentParser(prog="python -m lsf.manager", description=__doc__.split("\n")[0])
    cli.add_argument("--lsf-dir", required=True, type=Path)
    args = cli.parse_args(argv)
    return Manager(args.lsf_dir).run()


if __name__ == "__main__":
    sys.exit(main())
