"""Test doubles for the LSF layer: a synthetic counter probe, LSF job environments and a
fake scheduler that runs each job in-process through the real ``lsf.job`` entry point."""

import getpass
import itertools
import socket
import sys
from pathlib import Path

import pytest

from lsf import job as lsf_job
from lsf.cost_monitor import THRESHOLDS
from lsf.scheduler import JobStatus, SubmitResult

TESTS = Path(__file__).resolve().parents[2] / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from stage_world import make_world  # noqa: E402

# In-process jobs run here, so LSF's host must be this one.
HOST = socket.gethostname()
COST_REQUEST = (
    "select[(r1m < 10) && (ncpus == 56)] order[r15s:pg] rusage[mem=16.00] span[hosts=1] "
    "affinity[core(1,exclusive=(core,alljobs))*1]"
)


def lsf_environment(slots=16, request="span[hosts=1] rusage[mem=32]", job_id="4242", **extra):
    return {
        "LSB_JOBID": job_id,
        "LSB_JOBNAME": extra.pop("name", "qtb-test"),
        "LSB_QUEUE": "normal",
        "LSB_MCPU_HOSTS": f"{HOST} {slots}",
        "LSB_HOSTS": " ".join([HOST] * slots),
        "LSB_DJOB_NUMPROC": str(slots),
        "LSB_EFFECTIVE_RSRCREQ": request,
        **extra,
    }


def cost_context(**overrides):
    from lsf.context import from_environment

    return from_environment(lsf_environment(9, COST_REQUEST, **overrides), host=HOST)


class SyntheticProbe:
    """A host with SMT pairs (cpu n and n + 56), a virtual clock and scripted counters.

    ``foreign(t)`` is the foreign busy fraction of the worker core at virtual time ``t``,
    ``preemption(t)`` the worker's involuntary switches per second. The worker, while it
    runs, uses ``worker_rate`` CPU seconds per second.
    """

    def __init__(self, mask=None, cores=56, smt=True, foreign=None, preemption=None):
        self.cores = cores
        self.smt = smt
        self.mask = set(mask if mask is not None else self.cpus_of(range(9)))
        self.t = 0.0
        self.foreign = foreign or (lambda t: 0.0)
        self.preemption = preemption or (lambda t: 0.5)
        self.worker_rate = 0.98
        self.busy = {}
        self.worker_cpu = 0.0
        self.switches = 0.0
        self.running = None
        self.affinities = {}
        self.dead_thread_switches = 0
        self.broken = set()
        self.children = [0.0, 0]
        self.worker_tasks = None

    def cpus_of(self, cores):
        cpus = []
        for core in cores:
            cpus += [core, core + self.cores] if self.smt else [core]
        return cpus

    # The probe interface

    def unavailable(self):
        return None

    def now(self):
        return self.t

    def own_pid(self):
        return 1

    def sleep(self, seconds):
        self.advance(seconds)

    def online_cpus(self):
        return self.cpus_of(range(self.cores))

    def siblings(self, cpu):
        if "topology" in self.broken:
            return None
        core = cpu % self.cores
        return [core, core + self.cores] if self.smt else [core]

    def cpu_model(self):
        return "Synthetic Xeon"

    def affinity(self, tid=0):
        if tid in (0, 1):
            return set(self.affinities.get(1, self.mask))
        return set(self.affinities.get(tid, self.affinities.get("worker", self.mask)))

    def set_affinity(self, tid, cpus):
        self.affinities[tid] = set(cpus)

    def tasks(self, pid):
        if pid == 1:
            return [1]
        return self.worker_tasks or [pid]

    def spawn_options(self, cpus):
        self.affinities["worker"] = set(cpus)
        return {}

    def cpu_busy(self, cpus):
        if "counters" in self.broken:
            raise OSError("no /proc/stat")
        return sum(self.busy.get(cpu, 0.0) for cpu in cpus)

    def process_cpu(self, pid):
        return {pid: self.worker_cpu}

    def involuntary(self, pid):
        return {f"{pid}/{pid}": int(self.switches) - self.dead_thread_switches}

    def children_usage(self):
        return tuple(self.children)

    # Scripting

    def core_ids(self):
        return sorted({cpu % self.cores for cpu in self.mask})

    def worker_cpus(self):
        return sorted(self.affinities.get("worker", ()))

    def start(self, pid):
        self.running = pid
        self.worker_cpu = 0.0
        self.switches = 0.0

    def stop(self):
        self.children[0] += self.worker_cpu
        self.children[1] += int(self.switches)
        self.running = None

    def advance(self, seconds, steps=None):
        steps = steps or max(1, round(seconds / 0.05))
        for _ in range(steps):
            dt = seconds / steps
            core = self.worker_cpus()[:1] or self.cpus_of([self.core_ids()[1]])[:1]
            foreign = self.foreign(self.t) * dt
            for cpu in core:
                self.busy[cpu] = self.busy.get(cpu, 0.0) + foreign
                if self.running is not None:
                    self.busy[cpu] += self.worker_rate * dt
            if self.running is not None:
                self.worker_cpu += self.worker_rate * dt
                self.switches += self.preemption(self.t) * dt
            self.t += dt


def drive(hooks, probe, pid, seconds, entries=3, tick=0.1, measured_entries=None):
    """Run one synthetic worker process through its lifecycle hooks, like ``run_worker``."""
    hooks.spawn_options()
    probe.start(pid)
    hooks.launched(pid)
    if measured_entries is not None:
        for _ in range(measured_entries):
            problem = hooks.measurement(pid, b"B")
            if problem is None:
                probe.advance(seconds / measured_entries)
                problem = hooks.measurement(pid, b"E")
            if problem is not None:
                probe.stop()
                hooks.reaped(pid, -9)
                return problem
        probe.advance(0.001)
        hooks.exited(pid)
        probe.stop()
        return hooks.reaped(pid, 0)
    elapsed, done = 0.0, 0
    while elapsed < seconds - 1e-9:
        probe.advance(tick)
        elapsed += tick
        done = min(entries, int(entries * elapsed / seconds))
        problem = hooks.progress(pid, done)
        if problem is not None:
            probe.stop()
            hooks.reaped(pid, -9)
            return problem
    hooks.exited(pid)
    probe.stop()
    return hooks.reaped(pid, 0)


@pytest.fixture
def probe():
    return SyntheticProbe()


@pytest.fixture
def fast_thresholds(monkeypatch):
    monkeypatch.setitem(THRESHOLDS, "idle_probe_s", 0.5)
    return THRESHOLDS


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


class FakeScheduler:
    """``LsfBackend`` in memory. A job runs when it is first seen running, in-process.

    ``runner(spec, environ)`` returns the job's exit code; by default it calls
    ``lsf.job.main`` with the environment LSF would give the job. ``pend_polls`` delays the
    start; ``script`` maps a job-name substring to a list of submit outcomes to return first.
    """

    def __init__(self, runner=None, pend_polls=1, user=None):
        self.jobs = {}
        self.ids = itertools.count(1000)
        self.runner = runner or self.run_job
        self.pend_polls = pend_polls
        self.user = user or getpass.getuser()
        self.script = {}
        self.submitted = []
        self.cancelled = []
        self.query_failures = 0
        self.probe_factory = SyntheticProbe
        self.forget_after_run = False
        # Jobs whose name contains one of these stay RUN until released.
        self.hold = set()
        # Called with the job's spec when it starts running (e.g. to kill the manager).
        self.on_start = None

    def missing_commands(self):
        return []

    def submit(self, spec):
        self.submitted.append(spec)
        for fragment, outcomes in self.script.items():
            if fragment in spec.name and outcomes:
                outcome = outcomes.pop(0)
                if outcome == "rejected":
                    return SubmitResult(
                        "rejected", None, 255, "", "Bad resource", 0.01, spec.argv()
                    )
                if outcome == "lost":  # accepted by LSF, the answer never arrived
                    job_id = str(next(self.ids))
                    self.jobs[job_id] = self._job(spec, job_id)
                    return SubmitResult("ambiguous", None, None, "", "timed out", 0.01, spec.argv())
        job_id = str(next(self.ids))
        self.jobs[job_id] = self._job(spec, job_id)
        return SubmitResult(
            "accepted",
            job_id,
            0,
            f"Job <{job_id}> is submitted to queue <normal>.",
            "",
            0.01,
            spec.argv(),
        )

    def _job(self, spec, job_id):
        return {"spec": spec, "state": "PEND", "polls": 0, "exit": None, "host": None}

    def query(self, job_id):
        from lsf.scheduler import QueryFailed

        if self.query_failures:
            self.query_failures -= 1
            raise QueryFailed("mbatchd is busy")
        job = self.jobs.get(job_id)
        if job is None or job.get("forgotten"):
            return JobStatus(job_id, "NOTFOUND")
        if job["state"] == "PEND":
            job["polls"] += 1
            if job["polls"] > self.pend_polls:
                self.start(job_id)
        elif job["state"] == "RUN" and job["spec"].kind != "manager":
            if not any(h in job["spec"].name for h in self.hold):
                self.finish(job_id)
        return self.status(job_id)

    def status(self, job_id):
        job = self.jobs[job_id]
        return JobStatus(
            job_id,
            job["state"],
            job["exit"],
            job["host"],
            "Not enough hosts" if job["state"] == "PEND" else None,
            job["spec"].name,
            self.user,
        )

    def start(self, job_id):
        job = self.jobs[job_id]
        job["state"], job["host"] = "RUN", HOST
        if self.on_start:
            self.on_start(job["spec"])
        if job["spec"].kind == "manager" or any(h in job["spec"].name for h in self.hold):
            return
        self.finish(job_id)

    def finish(self, job_id):
        job = self.jobs[job_id]
        code = self.runner(job["spec"], self.environment(job_id))
        job["exit"] = code
        job["state"] = "DONE" if code == 0 else "EXIT"
        if self.forget_after_run:
            job["forgotten"] = True

    def environment(self, job_id):
        spec = self.jobs[job_id]["spec"]
        argv = spec.argv()
        slots = int(argv[argv.index("-n") + 1])
        request = argv[argv.index("-R") + 1]
        return lsf_environment(slots, request, job_id=job_id, name=spec.name)

    def run_job(self, spec, environ):
        argv = spec.command[spec.command.index("lsf.job") + 1 :]
        probe = self.probe_factory() if spec.kind == "cost" else None
        return lsf_job.main(argv, environ=environ, probe=probe)

    def find(self, name):
        return [self.status(i) for i, job in self.jobs.items() if job["spec"].name == name]

    def history(self, job_id):
        job = self.jobs.get(job_id)
        if job and job["state"] in {"DONE", "EXIT"}:
            return JobStatus(job_id, job["state"], job["exit"])
        return None

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        job = self.jobs.get(job_id)
        if job and job["state"] not in {"DONE", "EXIT"}:
            job["state"], job["exit"] = "EXIT", 130
        return True, f"Job <{job_id}> is being terminated"
