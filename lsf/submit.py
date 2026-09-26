"""Start one benchmark session on LSF: validate everything, then submit the manager job.

    uv run python lsf/submit.py --store DIR --results-root NEW_SESSION \\
        --baseline QISKIT_A --evolved QISKIT_B --cost-ncpus 56

Explicit arguments win over the ``QTB_STORE``/``QTB_SESSION``/``LSF_LOG_LEVEL`` fallbacks,
which win over the defaults. ``--results-root`` is the exact directory of this session's
data: it must not exist yet, and ``compile`` creates it. The orchestration records go to
the sibling ``<results-root>.lsf/``. Allocations are fixed (16 slots, 9 exclusive cores for
cost) and so is the internal limit of 20 cost noise retries. A successful submission means
LSF accepted the manager, not that the benchmark finished.
"""

if __name__ == "__main__" and not __package__:
    # Run as a file: this directory must not shadow the standard library (lsf/logging.py).
    import os as _os
    import sys as _sys

    _here = _os.path.dirname(_os.path.abspath(__file__))
    _sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.curdir) != _here]
    _sys.path.insert(0, _os.path.dirname(_here))

import argparse
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from qtb.canonical import write_json
from qtb.coordinator.storage import locked

from lsf import logging as log
from lsf import lsf_directory, measurement_identity, retry
from lsf.job import log_directory
from lsf.scheduler import JobSpec, LsfBackend, slots, wall_seconds

EXIT_OK, EXIT_ERROR, EXIT_PRECONDITION, EXIT_USAGE = 0, 40, 41, 64
PROFILES = ("iterations-profile", "confirm-profile")
JOBS = ("compile", "quality", "correctness", "unit-tests", "cost", "clean", "manager")
# Site starting points: check them against your queues' limits before relying on them.
DEFAULT_MEM_GB = {
    "compile": 64,
    "quality": 32,
    "correctness": 32,
    "unit-tests": 32,
    "cost": 16,
    "clean": 4,
    "manager": 4,
}
DEFAULT_WALL = {
    "compile": "6:00",
    "quality": "12:00",
    "correctness": "12:00",
    "unit-tests": "10:00",
    "cost": "8:00",
    "clean": "1:00",
    "manager": "168:00",
}
DEFAULT_MAX_R1M = 10.0
MODEL = re.compile(r"^[A-Za-z0-9_.\-]+$")
QUEUE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class Refused(Exception):
    def __init__(self, message, code=EXIT_USAGE):
        super().__init__(message)
        self.code = code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def parser():
    cli = Parser(
        prog="python lsf/submit.py",
        description="Start one benchmark session on LSF: validate the configuration, then "
        "submit the manager job that runs every stage.",
        epilog="Exit status: 0 the manager was accepted, 41 a precondition is not met (an "
        "existing session, a missing path or command), 40 the submission failed, 64 usage.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    inputs = cli.add_argument_group("session inputs")
    inputs.add_argument(
        "--baseline", required=True, type=Path, help="baseline Qiskit source checkout"
    )
    inputs.add_argument("--evolved", required=True, type=Path, help="evolved Qiskit checkout")
    inputs.add_argument("--profile", choices=PROFILES, default="iterations-profile")
    paths = cli.add_argument_group(
        "shared paths", "both on a filesystem mounted at the same path on every node"
    )
    paths.add_argument(
        "--store", type=Path, help="existing baseline store (fallback: QTB_STORE; required)"
    )
    paths.add_argument(
        "--results-root",
        type=Path,
        help="the exact, not yet existing directory of this session's data; its parent must "
        "exist (fallback: QTB_SESSION; required)",
    )
    execution = cli.add_argument_group("execution")
    execution.add_argument(
        "--no-unit-tests", action="store_true", help="do not run the optional upstream tests"
    )
    resources = cli.add_argument_group(
        "cluster resources",
        "Slots are fixed: 16 for every job, 9 exclusive physical cores for cost. Memory is "
        "in GB (rusage[mem=]); walls are LSF run limits, [hours:]minutes.",
    )
    resources.add_argument("--queue", help="queue for every job (default: the site's)")
    for job in JOBS:
        resources.add_argument(f"--{job}-queue", metavar="QUEUE", help=f"queue for {job}")
        resources.add_argument(
            f"--{job}-mem-gb",
            metavar="GB",
            type=float,
            default=DEFAULT_MEM_GB[job],
            help=f"memory for {job}",
        )
        resources.add_argument(
            f"--{job}-wall", metavar="H:MM", default=DEFAULT_WALL[job], help=f"run limit for {job}"
        )
    quiet = cli.add_argument_group(
        "quiet-node selection",
        "The approved hardware tier for cost: at least one of --cost-model and --cost-ncpus. "
        "The load limit applies at dispatch only; the job's own monitor checks the cores.",
    )
    quiet.add_argument("--cost-model", help="LSF host model (select[model == M])")
    quiet.add_argument(
        "--cost-ncpus", type=int, help="physical cores of the host (select[ncpus == N])"
    )
    quiet.add_argument(
        "--cost-max-r1m",
        type=float,
        default=DEFAULT_MAX_R1M,
        help="dispatch-time limit on the host's 1-minute run queue (select[r1m < L])",
    )
    logging_ = cli.add_argument_group("logging")
    logging_.add_argument(
        "--log-level",
        choices=log.LEVELS,
        type=str.upper,
        help="level for the launcher, manager and every job (fallback: LSF_LOG_LEVEL; "
        "default INFO)",
    )
    return cli


def _existing_directory(path, what):
    if not path.is_dir():
        raise Refused(f"{what} {path} is not an existing directory", EXIT_PRECONDITION)
    return str(path)


def resolve(args, environ=None):
    """The effective configuration; ``Refused`` when it cannot be used."""
    environ = os.environ if environ is None else environ
    store = args.store or environ.get("QTB_STORE")
    session = args.results_root or environ.get("QTB_SESSION")
    if not store:
        raise Refused("pass --store DIR (or set QTB_STORE)")
    if not session:
        raise Refused("pass --results-root DIR, the new session's directory (or set QTB_SESSION)")
    store = Path(store).expanduser().resolve()
    session = Path(session).expanduser().resolve()
    _existing_directory(store, "The store")
    if session.exists():
        raise Refused(
            f"{session} already exists: each launch starts a new session; pass a new "
            "--results-root",
            EXIT_PRECONDITION,
        )
    if not session.parent.is_dir():
        raise Refused(
            f"The session's parent {session.parent} does not exist; create it first",
            EXIT_PRECONDITION,
        )
    lsf_dir = lsf_directory(session)
    if lsf_dir.exists():
        raise Refused(f"{lsf_dir} already exists; pass a new --results-root", EXIT_PRECONDITION)
    baseline = _existing_directory(args.baseline.expanduser().resolve(), "The baseline")
    evolved = _existing_directory(args.evolved.expanduser().resolve(), "The evolved checkout")
    if args.cost_model is None and args.cost_ncpus is None:
        raise Refused("choose the approved cost hardware tier: --cost-model and/or --cost-ncpus")
    if args.cost_model is not None and not MODEL.match(args.cost_model):
        raise Refused(f"invalid --cost-model {args.cost_model!r}")
    if args.cost_ncpus is not None and args.cost_ncpus <= slots("cost"):
        raise Refused(f"--cost-ncpus must exceed the {slots('cost')} cores cost reserves")
    if not args.cost_max_r1m > 0:
        raise Refused("--cost-max-r1m must be positive")
    resources = {}
    for job in JOBS:
        key = job.replace("-", "_")
        queue = getattr(args, f"{key}_queue") or args.queue
        if queue is not None and not QUEUE.match(queue):
            raise Refused(f"invalid queue name {queue!r} for {job}")
        mem = getattr(args, f"{key}_mem_gb")
        if not mem > 0:
            raise Refused(f"--{job}-mem-gb must be positive")
        wall = getattr(args, f"{key}_wall")
        try:
            wall_seconds(wall)
        except ValueError as exc:
            raise Refused(f"--{job}-wall: {exc}") from exc
        resources[job] = {"queue": queue, "mem_gb": mem, "wall": wall, "slots": slots(job)}
    manager_wall = wall_seconds(resources["manager"]["wall"])
    longest = max(wall_seconds(resources[j]["wall"]) for j in JOBS if j != "manager")
    if manager_wall <= longest:
        raise Refused("--manager-wall must be longer than every stage's wall limit")
    try:
        level = log.resolve_level(args.log_level, environ)
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    return {
        "format": "qtb-lsf-launch/1",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "session": str(session),
        "lsf_dir": str(lsf_dir),
        "store": str(store),
        "baseline": baseline,
        "evolved": evolved,
        "profile": args.profile,
        "unit_tests": not args.no_unit_tests,
        "python": sys.executable,
        "project": str(Path(__file__).resolve().parents[1]),
        "log_level": level,
        "resources": resources,
        "cost_selector": {
            "model": args.cost_model,
            "ncpus": args.cost_ncpus,
            "max_r1m": args.cost_max_r1m,
        },
        "tier": {"model": args.cost_model, "ncpus": args.cost_ncpus},
        "max_cost_retries": retry.MAX_COST_RETRIES,
        "measurement_identity": measurement_identity(),
    }


def manager_spec(config):
    lsf_dir = Path(config["lsf_dir"])
    resources = config["resources"]["manager"]
    return JobSpec(
        kind="manager",
        name=f"qtb-{config['run_id']}-manager",
        command=[config["python"], "-P", "-m", "lsf.manager", "--lsf-dir", str(lsf_dir)],
        queue=resources["queue"],
        mem_gb=resources["mem_gb"],
        wall=resources["wall"],
        output=str(log_directory(lsf_dir, config["run_id"]) / "manager.%J.out"),
        # A manager requeued after a host failure reconciles the ledger and carries on.
        rerunnable=True,
    )


def submit(config, backend):
    """Create the orchestration records, then submit the manager; returns its job ID."""
    lsf_dir = Path(config["lsf_dir"])
    lsf_dir.mkdir()
    # The manager can start before bsub returns. Keep its first ledger read behind
    # the launcher's final update, otherwise a stale launcher snapshot erases jobs.
    with locked(lsf_dir / "orchestration.lock"):
        return _submit_locked(config, backend, lsf_dir)


def _submit_locked(config, backend, lsf_dir):
    write_json(lsf_dir / "launch.json", config)
    logs = log_directory(lsf_dir, config["run_id"])
    (logs / "jobs").mkdir(parents=True, exist_ok=True)
    log.setup(logs, "launcher", config["log_level"], "launcher")
    log.bind(run_id=config["run_id"], session=config["session"])
    log.event("launch.config", "effective configuration", config=config)
    ledger = retry.Ledger.create(
        lsf_dir / "ledger.json",
        run_id=config["run_id"],
        session=config["session"],
        lsf_dir=lsf_dir,
    )
    spec = manager_spec(config)
    argv = spec.argv()
    ledger.reserve("manager", stage="manager", name=spec.name, command=argv, output=spec.output)
    log.event("submit.intent", log.quote(argv), job_name=spec.name, argv=argv)
    log.flush()
    result = backend.submit(spec)
    log.event(
        f"submit.{result.outcome}",
        (result.stdout + result.stderr).strip() or result.outcome,
        level="INFO" if result.outcome == "accepted" else "ERROR",
        job_id=result.job_id,
        returncode=result.returncode,
        seconds=result.seconds,
    )
    status = {"accepted": "submitted", "rejected": "rejected"}.get(result.outcome, "ambiguous")
    ledger.update("manager", status=status, job_id=result.job_id)
    log.flush()
    if result.outcome != "accepted":
        detail = (result.stdout + result.stderr).strip()
        raise Refused(
            f"LSF did not accept the manager ({result.outcome}): {detail}"
            + (
                f"; check `bjobs -a -J {spec.name}` before starting again"
                if result.outcome == "ambiguous"
                else ""
            ),
            EXIT_ERROR,
        )
    return result.job_id


def main(argv=None, backend=None, environ=None):
    args = parser().parse_args(argv)
    backend = backend or LsfBackend()
    try:
        config = resolve(args, environ)
        missing = backend.missing_commands()
        if missing:
            raise Refused(
                f"LSF commands not found: {', '.join(missing)}; run on a login node",
                EXIT_PRECONDITION,
            )
        job_id = submit(config, backend)
    except Refused as exc:
        print(f"{'ERROR' if exc.code == EXIT_ERROR else 'REFUSED'}: {exc}", file=sys.stderr)
        log.close()
        return exc.code
    logs = log_directory(config["lsf_dir"], config["run_id"])
    print(
        "\n".join(
            [
                f"Submitted manager job {job_id} (LSF accepted it; the benchmark has not run yet).",
                f"  session:      {config['session']}",
                f"  run:          {config['run_id']}",
                f"  logs:         {logs}",
                f"  manager log:  {logs / 'manager-1.log'} (a requeued manager: manager-2.log)",
                f"  ledger:       {Path(config['lsf_dir']) / 'ledger.json'}",
                f"  report:       {Path(config['lsf_dir']) / 'report.md'}",
                f"  watch:        bjobs -a -J 'qtb-{config['run_id']}-*'",
                f"  stop:         python -m lsf.control stop --results-root {config['session']}",
            ]
        )
    )
    log.close()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
