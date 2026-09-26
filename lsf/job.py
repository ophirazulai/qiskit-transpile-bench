"""Entry point of every stage job the manager submits: ``python -P -m lsf.job``.

It translates the LSF allocation into the harness's execution context (``lsf.context``),
installs what the stage needs and runs exactly one harness command:

- ``compile`` records the session's cost-evidence requirement (``lsf.cost_evidence``);
- ``cost`` installs the cost monitor (``lsf.cost_monitor``) with this attempt's identity;
- every stage records the job's identity as its invocation, which the manager checks
  before it trusts a stage outcome.

It then writes ``outcomes/<job key>.json`` (the exit status and the committed stage state)
and exits with the harness's status. A termination signal kills the stage's worker process
groups too, since they run in sessions of their own.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from qtb.canonical import read_json, write_json
from qtb.errors import HarnessError
from qtb.execution import installed

from lsf import logging as log
from lsf.context import allocation_problems, execution_for, from_environment
from lsf.scheduler import slots

OUTCOME_FORMAT = "qtb-lsf-outcome/1"
EXIT_ERROR, EXIT_PRECONDITION = 40, 41
STAGES = ("compile", "quality", "correctness", "unit-tests", "cost", "clean")
SIGNALS = ("SIGTERM", "SIGINT", "SIGHUP", "SIGUSR2", "SIGXCPU")


class Terminated(BaseException):
    """A termination signal; not an ``Exception``, so no stage records it as a crash."""

    def __init__(self, signum):
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


def outcome_path(lsf_dir, key):
    return Path(lsf_dir) / "outcomes" / f"{key}.json"


def log_directory(lsf_dir, run_id):
    return Path(lsf_dir) / "logs" / run_id


def descendants(pid):
    """``[(pid, pgid)]`` of every process below ``pid``."""
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True, check=False
    ).stdout
    table = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            table[int(parts[0])] = (int(parts[1]), int(parts[2]))
    found, frontier = [], [pid]
    while frontier:
        parent = frontier.pop()
        children = [(child, group) for child, (owner, group) in table.items() if owner == parent]
        found += children
        frontier += [child for child, _ in children]
    return found


def kill_descendants():
    """Kill every descendant and each process group of its own (worker sessions)."""
    own_group = os.getpgid(0)
    killed = []
    for pid, group in descendants(os.getpid()):
        for kill, target in ((os.killpg, group), (os.kill, pid)):
            if kill is os.killpg and target == own_group:
                continue
            try:
                kill(target, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        killed.append(pid)
    return killed


def _terminate(signum, _frame):
    killed = kill_descendants()
    log.warning("job.signal", f"{signal.Signals(signum).name}: stopping the stage", killed=killed)
    log.flush()
    raise Terminated(signum)


def parser():
    cli = argparse.ArgumentParser(prog="python -m lsf.job", description=__doc__.split("\n")[0])
    cli.add_argument("--lsf-dir", required=True, type=Path)
    cli.add_argument("--job-key", required=True)
    cli.add_argument("--stage", required=True, choices=STAGES)
    cli.add_argument("--attempt", type=int)
    cli.add_argument("--log-level")
    return cli


def _state(session, stage):
    from qtb.coordinator.stages import clean_status, read_state

    try:
        if stage == "clean":
            status = clean_status(session) if session.exists() else None
            return {"status": status} if status else None
        state = read_state(session, stage) if session.exists() else None
    except (HarnessError, OSError, ValueError) as exc:
        return {"status": "unreadable", "reason": str(exc)}
    if state is None:
        return None
    keep = ("status", "invocation", "reason", "contamination", "gate", "attempts", "reused")
    return {key: state.get(key) for key in keep if key in state}


def _monitor(session, key):
    """Where a cost invocation's placement and pre-flight evidence are, for the ledger."""
    path = session / "cost" / "monitor" / key / "preflight.json"
    try:
        preflight = read_json(path)
    except (HarnessError, OSError):
        return None
    return {
        "preflight": str(path),
        "machine": preflight.get("machine"),
        "layout": {k: (preflight.get("layout") or {}).get(k) for k in ("monitor", "worker")},
        "tier": preflight.get("tier"),
        "idle_probe": preflight.get("idle_probe"),
    }


def _execution(stage, context, launch, invocation, session, probe):
    if stage == "cost":
        from lsf.cost_monitor import CostMonitor

        monitor = CostMonitor(
            context,
            tier=launch["tier"],
            attempt=invocation,
            diagnostics=session / "cost" / "monitor" / invocation["job_key"],
            probe=probe,
        )
        return execution_for(context, invocation=invocation, cost_monitor=monitor)
    extensions = {}
    if stage == "compile":
        from lsf.cost_evidence import requirement

        extensions["session"] = {"cost_evidence": requirement(launch["tier"])}
    return execution_for(context, invocation=invocation, workers=context["slots"], **extensions)


def run(args, environ=None, probe=None):
    """Run the stage; returns ``(exit status, message)``."""
    from qtb.cli import invoke

    lsf_dir = args.lsf_dir.resolve()
    launch = read_json(lsf_dir / "launch.json")
    session = Path(launch["session"])
    context = from_environment(environ)
    log.event(
        "job.allocation",
        f"{context.get('slots')} slots on {context.get('host')}",
        allocation=context,
    )
    problems = allocation_problems(context, slots=slots(args.stage))
    if problems:
        return EXIT_PRECONDITION, "PRECONDITION: " + "; ".join(problems)
    invocation = {
        "run_id": launch["run_id"],
        "job_key": args.job_key,
        "attempt": args.attempt,
        "job_id": context.get("job_id"),
        "job_name": context.get("job_name"),
    }
    execution = _execution(args.stage, context, launch, invocation, session, probe)
    extra = {}
    if args.stage == "compile":
        extra = dict(
            baseline=launch["baseline"],
            evolved=launch["evolved"],
            profile=launch["profile"],
            store=launch["store"],
        )
    with installed(execution):
        return invoke(args.stage, session, **extra)


def main(argv=None, environ=None, probe=None):
    args = parser().parse_args(argv)
    lsf_dir = args.lsf_dir.resolve()
    launch = read_json(lsf_dir / "launch.json")
    level = log.resolve_level(args.log_level)
    name = f"job-{args.job_key}"
    log.setup(log_directory(lsf_dir, launch["run_id"]) / "jobs", name, level, "job")
    environ = os.environ if environ is None else environ
    log.bind(
        run_id=launch["run_id"],
        session=launch["session"],
        stage=args.stage,
        attempt=args.attempt,
        job_key=args.job_key,
        job_id=environ.get("LSB_JOBID"),
        job_name=environ.get("LSB_JOBNAME"),
    )
    started, clock = datetime.now(UTC).isoformat(), time.monotonic()
    log.event("job.started", f"{args.stage} job {args.job_key}", argv=sys.argv)
    previous = {sig: signal.signal(getattr(signal, sig), _terminate) for sig in SIGNALS}
    message, terminated = None, None
    try:
        code, message = run(args, environ, probe)
    except Terminated as exc:
        code, terminated = 128 + exc.signum, exc.args[0]
    except Exception as exc:  # the wrapper itself failed; the manager must see why
        log.error("job.error", f"{type(exc).__name__}: {exc}", exc_info=True)
        code, message = EXIT_ERROR, f"ERROR: {type(exc).__name__}: {exc}"
    finally:
        for sig, handler in previous.items():
            signal.signal(getattr(signal, sig), handler)
    if message:
        print(message, file=sys.stderr)
    outcome = {
        "format": OUTCOME_FORMAT,
        "job_key": args.job_key,
        "stage": args.stage,
        "attempt": args.attempt,
        "exit_code": code,
        "message": message,
        "terminated": terminated,
        "state": _state(Path(launch["session"]), args.stage),
        "monitor": _monitor(Path(launch["session"]), args.job_key)
        if args.stage == "cost"
        else None,
        "host": log.bound().get("host"),
        "job_id": environ.get("LSB_JOBID"),
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "seconds": time.monotonic() - clock,
        "logs": {k: str(v) for k, v in log.paths().items()},
    }
    write_json(outcome_path(lsf_dir, args.job_key), outcome)
    level_name = "INFO" if code in (0, 42) else "WARNING"
    log.event(
        "job.finished",
        f"{args.stage} exited {code}" + (f": {message}" if message else ""),
        level=level_name,
        exit_code=code,
        state=outcome["state"],
        seconds=outcome["seconds"],
    )
    log.flush()
    log.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
