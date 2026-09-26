"""LSF commands and resource requests: submit, poll, look up by name, cancel.

Every command's argv, duration, return code and response are logged; a large response is
kept in a diagnostic file next to the log. Results are explicit so that submission errors,
queue delays, signal deaths and stage exit codes are never confused:

- ``submit`` returns ``accepted`` (with the job ID), ``rejected`` (``bsub`` refused it: no
  job exists) or ``ambiguous`` (no clear answer: the job may exist and must be found by
  its unique name before anything else is submitted for it);
- ``query`` returns a ``JobStatus``; a failed query raises ``QueryFailed``, which is never
  proof that a job has gone. ``NOTFOUND`` means ``bjobs`` no longer lists it.

Allocations are fixed: 16 slots for every job except cost, which has 9 exclusive physical
cores on one quiet host of the approved hardware tier. Memory requests are in GB, as the
site's ``LSF_UNIT_FOR_LIMITS`` is.
"""

import getpass
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from lsf import logging as log

NON_COST_SLOTS = 16
COST_SLOTS = 9
KINDS = ("manager", "compile", "quality", "correctness", "unit-tests", "cost", "clean")
TERMINAL = {"DONE", "EXIT"}
ACTIVE = {"PEND", "PSUSP", "RUN", "USUSP", "SSUSP", "WAIT", "PROV"}
FIELDS = "jobid stat exit_code exec_host pend_reason job_name user"
SUBMITTED = re.compile(r"Job <(\d+)> is submitted")
WALL = re.compile(r"^(\d+)(?::([0-5]?\d))?$")
LARGE = 4000


class QueryFailed(Exception):
    """A scheduler query failed; the job's state is unknown, not gone."""


def slots(kind):
    return COST_SLOTS if kind == "cost" else NON_COST_SLOTS


def wall_seconds(wall):
    """LSF's ``[hours:]minutes`` run limit in seconds."""
    match = WALL.match(str(wall))
    if not match:
        raise ValueError(f"Invalid wall limit {wall!r}; use [hours:]minutes, for example 12:00")
    if match.group(2) is None:
        return int(match.group(1)) * 60
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60


def resource_request(kind, mem_gb, selector=None):
    """The ``-R`` string: one host, memory, and for cost the quiet exclusive-core request."""
    parts = []
    if kind == "cost":
        terms = []
        if selector.get("max_r1m") is not None:
            terms.append(f"r1m < {selector['max_r1m']:g}")
        if selector.get("model"):
            terms.append(f"model == {selector['model']}")
        if selector.get("ncpus") is not None:
            terms.append(f"ncpus == {selector['ncpus']}")
        if terms:
            parts.append(f"select[{' && '.join(terms)}]")
    parts.append("span[hosts=1]")
    if kind == "cost":
        # Per slot: core(1) with nine slots is nine physical cores, SMT siblings included.
        parts.append("affinity[core(1,exclusive=(core,alljobs))]")
    parts.append(f"rusage[mem={mem_gb:g}]")
    return " ".join(parts)


@dataclass
class JobSpec:
    kind: str
    name: str
    command: list
    queue: str | None
    mem_gb: float
    wall: str
    output: str
    selector: dict = field(default_factory=dict)
    rerunnable: bool = False

    def argv(self):
        argv = ["bsub", "-J", self.name, "-n", str(slots(self.kind))]
        if self.queue:
            argv += ["-q", self.queue]
        argv += ["-W", self.wall, "-R", resource_request(self.kind, self.mem_gb, self.selector)]
        argv += ["-o", self.output]
        if self.rerunnable:
            argv.append("-r")
        if self.kind == "cost":
            argv += ["env", "THREADS=9"]
        return argv + [str(part) for part in self.command]


@dataclass
class JobStatus:
    job_id: str
    state: str
    exit_code: int | None = None
    host: str | None = None
    pending_reason: str | None = None
    name: str | None = None
    user: str | None = None

    @property
    def terminal(self):
        return self.state in TERMINAL


@dataclass
class SubmitResult:
    outcome: str
    job_id: str | None
    returncode: int | None
    stdout: str
    stderr: str
    seconds: float
    argv: list


def _exit_code(text):
    text = (text or "").strip()
    return int(text) if text.lstrip("-").isdigit() else None


def _host(text):
    text = (text or "").strip().split(":")[0]
    return text.split("*", 1)[-1] or None


def parse_bjobs(stdout):
    """``bjobs -json`` records as ``JobStatus`` (``NOTFOUND`` for a job it no longer knows)."""
    data = json.loads(stdout or "{}")
    statuses = []
    for row in data.get("RECORDS", []):
        if row.get("ERROR"):
            if "not found" in row["ERROR"].lower():
                statuses.append(JobStatus(str(row.get("JOBID", "")), "NOTFOUND"))
                continue
            raise QueryFailed(row["ERROR"])
        statuses.append(
            JobStatus(
                job_id=str(row.get("JOBID")),
                state=row.get("STAT") or "UNKWN",
                exit_code=_exit_code(row.get("EXIT_CODE")),
                host=_host(row.get("EXEC_HOST")),
                pending_reason=(row.get("PEND_REASON") or "").strip() or None,
                name=row.get("JOB_NAME"),
                user=row.get("USER"),
            )
        )
    return statuses


def parse_bhist(job_id, stdout):
    """A terminal ``JobStatus`` from ``bhist -l``, or ``None`` when it shows no end."""
    text = re.sub(r"\n\s{10,}", "", stdout or "")
    if "Done successfully" in text:
        return JobStatus(job_id, "DONE", 0)
    match = re.search(r"Exited with exit code (\d+)", text)
    if match:
        return JobStatus(job_id, "EXIT", int(match.group(1)))
    match = re.search(r"Exited by (?:LSF )?signal (\d+)", text)
    if match:
        return JobStatus(job_id, "EXIT", 128 + int(match.group(1)))
    if re.search(r"Completed <exit>|Exited;", text):
        return JobStatus(job_id, "EXIT")
    return None


class LsfBackend:
    """The real scheduler. ``runner`` is ``subprocess.run`` or a test double."""

    def __init__(self, runner=subprocess.run, timeout=120, diagnostics=None):
        self.runner = runner
        self.timeout = timeout
        self.diagnostics = Path(diagnostics) if diagnostics else None
        self.user = getpass.getuser()

    def missing_commands(self):
        import shutil

        return [c for c in ("bsub", "bjobs", "bhist", "bkill") if shutil.which(c) is None]

    def _run(self, argv, purpose):
        started = time.monotonic()
        log.debug("scheduler.command", log.quote(argv), argv=argv, purpose=purpose)
        try:
            proc = self.runner(
                argv, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            seconds = time.monotonic() - started
            log.warning(
                "scheduler.timeout",
                f"{argv[0]} gave no answer in {self.timeout} s",
                argv=argv,
                seconds=seconds,
            )
            raise TimeoutError(f"{argv[0]} timed out after {self.timeout} s") from exc
        seconds = time.monotonic() - started
        stdout, stderr = proc.stdout or "", proc.stderr or ""
        fields = dict(argv=argv, purpose=purpose, returncode=proc.returncode, seconds=seconds)
        if len(stdout) + len(stderr) > LARGE and self.diagnostics:
            path = self.diagnostics / f"{argv[0]}-{time.time_ns()}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"$ {log.quote(argv)}\n--- stdout\n{stdout}\n--- stderr\n{stderr}\n")
            fields["response_file"] = str(path)
        else:
            fields.update(stdout=stdout.strip(), stderr=stderr.strip())
        log.debug("scheduler.response", f"{argv[0]} exited {proc.returncode}", **fields)
        return proc.returncode, stdout, stderr, seconds

    def submit(self, spec):
        argv = spec.argv()
        log.event("submit.command", log.quote(argv), argv=argv, job_name=spec.name)
        log.flush()
        started = time.monotonic()
        try:
            code, stdout, stderr, seconds = self._run(argv, "submit")
        except (TimeoutError, OSError) as exc:
            return SubmitResult(
                "ambiguous", None, None, "", str(exc), time.monotonic() - started, argv
            )
        match = SUBMITTED.search(stdout)
        if match:
            return SubmitResult("accepted", match.group(1), code, stdout, stderr, seconds, argv)
        # A connection loss or signal can follow acceptance. Only an explicit refusal
        # proves that no job exists; everything else must be reconciled by nonce/name.
        refused = re.search(r"\bjob (?:was )?not submitted\b", stdout + stderr, re.I)
        outcome = "rejected" if code and refused else "ambiguous"
        return SubmitResult(outcome, None, code, stdout, stderr, seconds, argv)

    def _bjobs(self, target):
        argv = ["bjobs", "-a", "-json", "-o", FIELDS, *target]
        try:
            code, stdout, stderr, _ = self._run(argv, "query")
        except (TimeoutError, OSError) as exc:
            raise QueryFailed(str(exc)) from exc
        if not stdout.strip():
            if "not found" in stderr.lower() or "no job found" in stderr.lower():
                return []
            raise QueryFailed(stderr.strip() or f"bjobs exited {code} with no output")
        try:
            return parse_bjobs(stdout)
        except ValueError as exc:
            raise QueryFailed(f"unreadable bjobs output: {exc}") from exc

    def query(self, job_id):
        statuses = self._bjobs([str(job_id)])
        return statuses[0] if statuses else JobStatus(str(job_id), "NOTFOUND")

    def find(self, name):
        return [s for s in self._bjobs(["-J", name]) if s.state != "NOTFOUND"]

    def history(self, job_id):
        try:
            _, stdout, _, _ = self._run(["bhist", "-l", "-n", "0", str(job_id)], "history")
        except (TimeoutError, OSError):
            return None
        return parse_bhist(str(job_id), stdout)

    def cancel(self, job_id):
        try:
            code, stdout, stderr, _ = self._run(["bkill", str(job_id)], "cancel")
        except (TimeoutError, OSError) as exc:
            return False, str(exc)
        return code == 0 or "already finished" in (stdout + stderr), (stdout + stderr).strip()
