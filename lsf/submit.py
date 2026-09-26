"""Start one benchmark session on LSF: validate everything, then submit the manager job.

    uv run python lsf/submit.py --store DIR --results-root NEW_SESSION \\
        --baseline QISKIT_A --evolved QISKIT_B --cost-ncpus 56

Explicit arguments win over the ``QTB_STORE``/``QTB_SESSION``/``LSF_LOG_LEVEL`` fallbacks,
which win over the defaults. ``--results-root`` is the exact directory of this session's
data: it must not exist yet, and ``compile`` creates it. The orchestration records go to
the sibling ``<results-root>.lsf/``. Allocations are fixed (16 slots, 9 exclusive cores for
cost) and so is the internal limit of 20 cost noise retries. A successful submission means
LSF accepted the manager, not that the benchmark finished.

    uv run python lsf/submit.py --resume --results-root SESSION
    uv run python lsf/submit.py --resume --upgrade-cost --reason TEXT --results-root SESSION

``--resume`` continues a session this launcher started, after its manager ended: every
unfinished stage gets one new job and cost a fresh retry budget (``lsf.retry`` epochs). The
recorded inputs and resources are kept: other options may repeat them, not change them. It
is refused when the session is cleaned, while a manager or any job of the session is still
in LSF (``lsf.control stop``/``reap``), and when an unfinished stage is pinned to another
harness than this one. ``--upgrade-cost`` first re-pins an unfinished cost stage, and only
that, to this harness (``qtb.coordinator.amend``, recorded with ``--reason`` in
``run.json:amendments``); a finished cost stage is never measured again.
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

from qtb.canonical import read_json, write_json
from qtb.coordinator.storage import LockBusy, locked

from lsf import logging as log
from lsf import lsf_directory, measurement_identity, retry
from lsf.job import log_directory
from lsf.scheduler import JobSpec, LsfBackend, QueryFailed, slots, wall_seconds

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
        "existing session, a missing path or command, a session that cannot be resumed), 40 "
        "the submission failed, 64 usage.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    inputs = cli.add_argument_group("session inputs")
    inputs.add_argument("--baseline", type=Path, help="baseline Qiskit source checkout (required)")
    inputs.add_argument("--evolved", type=Path, help="evolved Qiskit checkout (required)")
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
    resume = cli.add_argument_group(
        "resuming",
        "Continue an existing session after its manager ended, with its recorded inputs and "
        "resources: pass --results-root, and optionally --log-level; any other option must "
        "repeat its recorded value.",
    )
    resume.add_argument(
        "--resume",
        action="store_true",
        help="submit a new manager for the session's unfinished stages; cost gets a fresh "
        "retry budget",
    )
    resume.add_argument(
        "--upgrade-cost",
        action="store_true",
        help="with --resume: re-pin the unfinished cost stage, and only it, to this harness "
        "(recorded in run.json:amendments; needs --reason)",
    )
    resume.add_argument("--reason", help="why cost is re-pinned (with --upgrade-cost)")
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
    if args.baseline is None or args.evolved is None:
        raise Refused("pass --baseline and --evolved, the checkouts to compare")
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


def manager_spec(config, key="manager"):
    lsf_dir = Path(config["lsf_dir"])
    resources = config["resources"]["manager"]
    return JobSpec(
        kind="manager",
        name=f"qtb-{config['run_id']}-{key}",
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
    return _submit_manager(config, backend, ledger, "manager")


def _submit_manager(config, backend, ledger, key):
    spec = manager_spec(config, key)
    argv = spec.argv()
    ledger.reserve(key, stage="manager", name=spec.name, command=argv, output=spec.output)
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
    ledger.update(key, status=status, job_id=result.job_id)
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


# Options a resume may pass; every other one keeps the recorded launch.
RESUME_OPTIONS = {"results_root", "log_level", "resume", "upgrade_cost", "reason"}
# The stage order a resume checks; unit-tests only when the launch runs it.
STAGES = ("compile", "quality", "correctness", "unit-tests", "cost")
# What a resume takes from the running launcher; the previous values are kept in the ledger.
REFRESHED = ("python", "project", "measurement_identity", "log_level")


def resume_target(args, environ=None):
    """The orchestration directory of the session to resume; ``Refused`` when there is none."""
    environ = os.environ if environ is None else environ
    if args.upgrade_cost and not (args.reason or "").strip():
        raise Refused("--upgrade-cost needs --reason: it is recorded in run.json:amendments")
    session = args.results_root or environ.get("QTB_SESSION")
    if not session:
        raise Refused("pass --results-root DIR, the session to resume (or set QTB_SESSION)")
    lsf_dir = lsf_directory(Path(session).expanduser().resolve())
    for name in ("launch.json", "ledger.json"):
        if not (lsf_dir / name).exists():
            raise Refused(
                f"{lsf_dir / name} does not exist: was this session launched with lsf/submit.py?",
                EXIT_PRECONDITION,
            )
    changed = _changed(args, read_json(lsf_dir / "launch.json"))
    if changed:
        options = ", ".join("--" + k.replace("_", "-") for k in changed)
        raise Refused(
            f"--resume keeps the recorded launch; {options} "
            f"{'differs' if len(changed) == 1 else 'differ'} from it and cannot be changed "
            "(repeat the recorded values or omit them)"
        )
    return lsf_dir


def _changed(args, launch):
    """The options given with ``--resume`` whose values differ from the recorded launch.

    An option left at its default is not given; one repeating the recorded value is not a
    change either.
    """
    defaults = vars(parser().parse_args([]))
    recorded = _recorded_options(launch)
    return sorted(
        k
        for k, v in vars(args).items()
        if k not in RESUME_OPTIONS and v != defaults[k] and _normal(v) != recorded.get(k)
    )


def _recorded_options(launch):
    """The command-line value of each option, as recorded in ``launch``."""
    selector = launch["cost_selector"]
    options = {
        "baseline": launch["baseline"],
        "evolved": launch["evolved"],
        "store": launch["store"],
        "profile": launch["profile"],
        "no_unit_tests": not launch["unit_tests"],
        "cost_model": selector["model"],
        "cost_ncpus": selector["ncpus"],
        "cost_max_r1m": selector["max_r1m"],
    }
    queues = {launch["resources"][job]["queue"] for job in JOBS}
    if len(queues) == 1:
        options["queue"] = queues.pop()
    for job in JOBS:
        key, recorded = job.replace("-", "_"), launch["resources"][job]
        options[f"{key}_queue"] = recorded["queue"]
        options[f"{key}_mem_gb"] = recorded["mem_gb"]
        options[f"{key}_wall"] = recorded["wall"]
    return options


def _normal(value):
    return str(value.expanduser().resolve()) if isinstance(value, Path) else value


def resume(args, backend, lsf_dir, environ=None, progress=print):
    """Submit a new manager for the session; returns ``(launch, job ID, resume record)``."""
    try:
        with locked(lsf_dir / "orchestration.lock", wait=False):
            return _resume_locked(args, backend, lsf_dir, environ, progress)
    except LockBusy as exc:
        raise Refused(
            "a manager holds the session; stop it first (python -m lsf.control stop)",
            EXIT_PRECONDITION,
        ) from exc


def _resume_locked(args, backend, lsf_dir, environ, progress):
    from qtb.coordinator.stages import clean_status

    environ = os.environ if environ is None else environ
    launch = read_json(lsf_dir / "launch.json")
    ledger = retry.Ledger.load(lsf_dir / "ledger.json")
    session = Path(launch["session"])
    if clean_status(session):
        raise Refused(
            f"{session} is cleaned (or cleaning); nothing can be measured again. "
            f"`qtb decide --results-root {session}` replays its verdict",
            EXIT_PRECONDITION,
        )
    _check_jobs_ended(ledger, backend, session)
    pending = _unfinished(launch, session)
    amendment = None
    if args.upgrade_cost:
        amendment = _upgrade_cost(launch, session, pending, args.reason, progress)
    _check_harness(session, pending)
    previous = {key: launch.get(key) for key in REFRESHED}
    explicit = args.log_level or environ.get("LSF_LOG_LEVEL")
    try:
        level = log.resolve_level(args.log_level, environ) if explicit else launch["log_level"]
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    launch.update(
        python=sys.executable,
        project=str(Path(__file__).resolve().parents[1]),
        measurement_identity=measurement_identity(),
        log_level=level,
    )
    write_json(lsf_dir / "launch.json", launch)
    key = f"manager-r{ledger.epoch + 1}"
    logs = log_directory(lsf_dir, launch["run_id"])
    log.setup(logs, f"resume-{ledger.epoch + 1}", level, "launcher")
    log.bind(run_id=launch["run_id"], session=launch["session"])
    record = ledger.begin_epoch(
        manager=key,
        pending=pending,
        amendment=amendment["index"] if amendment else None,
        launch={"previous": previous},
    )
    log.event(
        "resume.started",
        f"resume {record['index']}: {', '.join(pending) or 'decide'}",
        resume=record,
    )
    return launch, _submit_manager(launch, backend, ledger, key), record


def _check_jobs_ended(ledger, backend, session):
    """Refused while any job of the session may still be in LSF; the managers are settled."""
    reap = f"python -m lsf.control reap --results-root {session}"
    for entry in ledger.jobs():
        if entry["status"] not in retry.ACTIVE and entry["status"] != "unreconciled":
            continue
        if entry["stage"] != "manager":
            raise Refused(
                f"job {entry['key']} is {entry['status']} in the ledger; settle it first: {reap}",
                EXIT_PRECONDITION,
            )
        try:
            if entry.get("job_id"):
                status = backend.query(entry["job_id"])
                if status.state == "NOTFOUND":
                    status = backend.history(entry["job_id"]) or status
                found = [status]
            else:
                found = backend.find(entry["name"])
        except QueryFailed as exc:
            raise Refused(
                f"cannot check the manager job {entry.get('job_id') or entry['name']}: {exc}",
                EXIT_PRECONDITION,
            ) from exc
        live = [s for s in found if s.state != "NOTFOUND" and not s.terminal]
        if live:
            raise Refused(
                f"the manager job {live[0].job_id} is still {live[0].state}; stop it first "
                f"(python -m lsf.control stop --results-root {session})",
                EXIT_PRECONDITION,
            )
        state = found[0].state if found else "NOTFOUND"
        ledger.update(
            entry["key"],
            status="terminal",
            scheduler={"state": state, "exit_code": getattr(found[0], "exit_code", None)}
            if found
            else {"state": state},
            settled_at=retry.now(),
        )


def _unfinished(launch, session):
    """The stages a new manager would run, in order."""
    from qtb.coordinator.stages import FINAL, read_state

    if not (session / "run.json").exists():
        return list(STAGES if launch["unit_tests"] else [s for s in STAGES if s != "unit-tests"])
    pending = []
    for stage in STAGES:
        if stage == "unit-tests" and not launch["unit_tests"]:
            continue
        state = read_state(session, stage)
        if not state or state["status"] not in FINAL:
            pending.append(stage)
    return pending


def _upgrade_cost(launch, session, pending, reason, progress):
    from qtb.coordinator.amend import amend_cost
    from qtb.errors import HarnessError

    from lsf.cost_evidence import requirement

    if not (session / "run.json").exists():
        raise Refused("--upgrade-cost needs a compiled session", EXIT_PRECONDITION)
    try:
        return amend_cost(
            session, {"cost_evidence": requirement(launch["tier"])}, reason, progress=progress
        )
    except HarnessError as exc:
        raise Refused(f"--upgrade-cost: {exc}", EXIT_PRECONDITION) from exc


def _check_harness(session, pending):
    """Every unfinished stage must be pinned to this harness, or its stage job refuses."""
    from qtb.config import coordinator_identity, implementation_identity
    from qtb.coordinator.amend import effective

    if not (session / "run.json").exists():
        return
    run = read_json(session / "run.json")
    current = {"coordinator": coordinator_identity(), "implementation": implementation_identity()}
    for stage in pending:
        pinned = effective(run, stage)["hashes"]
        differ = [k for k, v in current.items() if pinned.get(k) not in (None, v)]
        if not differ:
            continue
        hint = (
            "; or pass --upgrade-cost --reason TEXT to re-pin cost to this harness"
            if pending == ["cost"]
            else ""
        )
        raise Refused(
            f"{stage} is pinned to another harness ({', '.join(differ)} identity differs); "
            f"resume with the harness wheel archived in {session / 'harness-wheel'}{hint}",
            EXIT_PRECONDITION,
        )


def main(argv=None, backend=None, environ=None):
    args = parser().parse_args(argv)
    backend = backend or LsfBackend()
    record = None
    try:
        if args.upgrade_cost and not args.resume:
            raise Refused("--upgrade-cost re-pins an existing session: pass --resume too")
        missing = backend.missing_commands()
        if args.resume:
            lsf_dir = resume_target(args, environ)
        else:
            config = resolve(args, environ)
        if missing:
            raise Refused(
                f"LSF commands not found: {', '.join(missing)}; run on a login node",
                EXIT_PRECONDITION,
            )
        if args.resume:
            config, job_id, record = resume(args, backend, lsf_dir, environ)
        else:
            job_id = submit(config, backend)
    except Refused as exc:
        print(f"{'ERROR' if exc.code == EXIT_ERROR else 'REFUSED'}: {exc}", file=sys.stderr)
        log.close()
        return exc.code
    logs = log_directory(config["lsf_dir"], config["run_id"])
    # The manager numbers its logs by its start among all of the session's managers.
    index = len(read_json(Path(config["lsf_dir"]) / "ledger.json")["managers"]) + 1
    if record:
        print(
            f"Resume {record['index']} of {config['session']}: "
            f"{', '.join(record['pending']) or 'no stage'} unfinished; the new manager runs "
            "them, then decide"
            + (f" (cost re-pinned: amendment {record['amendment']})" if record["amendment"] else "")
            + "."
        )
    print(
        "\n".join(
            [
                f"Submitted manager job {job_id} (LSF accepted it; the benchmark has not run yet).",
                f"  session:      {config['session']}",
                f"  run:          {config['run_id']}",
                f"  logs:         {logs}",
                f"  manager log:  {logs / f'manager-{index}.log'} (a requeued manager: "
                f"manager-{index + 1}.log)",
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
