"""Watch, stop and recover one LSF session: ``python -P -m lsf.control``.

    status --results-root S   the manager and every job it submitted, with live LSF states
    stop   --results-root S   cancel the manager; it cancels the jobs it owns, then exits
    reap   --results-root S   after a manager died (for example SIGKILL or a lost host):
                              cancel the jobs it left, after checking their ID, name and
                              user, and settle them in the ledger

``reap`` refuses while a manager holds the session's orchestration lock. Nothing here
submits work or resumes a pipeline; to measure again, launch a new session.
"""

import argparse
import getpass
import sys
from pathlib import Path

from qtb.canonical import read_json
from qtb.coordinator.storage import LockBusy

from lsf import lsf_directory
from lsf.retry import ACTIVE, Ledger
from lsf.scheduler import LsfBackend, QueryFailed

EXIT_OK, EXIT_ERROR, EXIT_PRECONDITION = 0, 40, 41


def _live(backend, entry):
    if not entry.get("job_id"):
        return None
    try:
        return backend.query(entry["job_id"])
    except QueryFailed as exc:
        return f"query failed: {exc}"


def status(lsf_dir, backend, out=print):
    ledger = Ledger.load(lsf_dir / "ledger.json")
    out(f"session {ledger.data['session']} (run {ledger.data['run_id']})")
    for entry in sorted(ledger.jobs(), key=lambda e: e["reserved_at"]):
        live = _live(backend, entry) if entry["status"] in ACTIVE else None
        shown = getattr(live, "state", live) or (entry.get("outcome") or {}).get("kind")
        reason = getattr(live, "pending_reason", None)
        out(
            f"  {entry['key']:<28} job {entry.get('job_id') or '—':>10}  {entry['status']:<12} "
            f"{shown or ''}{f'  ({reason})' if reason else ''}"
        )
    cost = ledger.data["cost"]
    if cost.get("exhausted"):
        out(f"  cost retries exhausted: {cost['exhausted']['reason']}")
    decide = ledger.data.get("decide") or {}
    if decide:
        cleanup = (ledger.data.get("cleanup") or {}).get("status")
        out(f"  verdict: {decide.get('status')}; cleanup: {cleanup}")
    out(f"  report: {lsf_dir / 'report.md'}")
    return EXIT_OK


def stop(lsf_dir, backend, out=print):
    ledger = Ledger.load(lsf_dir / "ledger.json")
    manager = ledger.latest("manager")
    if not manager or not manager.get("job_id"):
        out("No manager job is recorded; use reap for leftover jobs.")
        return EXIT_PRECONDITION
    live = _live(backend, manager)
    if not hasattr(live, "state") or live.terminal or live.state == "NOTFOUND":
        out(f"The manager job {manager['job_id']} is not running ({getattr(live, 'state', live)}).")
        out(
            f"Cancel its leftover jobs with: python -m lsf.control reap --results-root "
            f"{ledger.data['session']}"
        )
        return EXIT_OK
    if live.name != manager["name"] or live.user != getpass.getuser():
        out(f"Job {manager['job_id']} is {live.name} of {live.user}, not this session's manager.")
        return EXIT_PRECONDITION
    ok, response = backend.cancel(manager["job_id"])
    out(f"bkill {manager['job_id']}: {response}")
    out(
        "The manager cancels the jobs it owns before it exits. If it cannot (for example the "
        "host failed), run: python -m lsf.control reap --results-root "
        f"{ledger.data['session']}"
    )
    return EXIT_OK if ok else EXIT_ERROR


def reap(lsf_dir, backend, out=print):
    from lsf.manager import Manager

    try:
        cancelled = Manager(lsf_dir, backend, poll_s=2.0).reap()
    except LockBusy:
        out("A manager is running for this session: use stop, not reap.")
        return EXIT_PRECONDITION
    out(f"Cancelled {len(cancelled)} jobs: {', '.join(cancelled) or 'none'}.")
    ledger = Ledger.load(lsf_dir / "ledger.json")
    left = [
        e["key"]
        for e in ledger.jobs()
        if e["status"] in ACTIVE or e["status"] == "unreconciled"
    ]
    if left:
        out(f"Still unconfirmed: {', '.join(left)}; run reap again later.")
        return EXIT_ERROR
    return EXIT_OK


def main(argv=None, backend=None, out=print):
    cli = argparse.ArgumentParser(prog="python -m lsf.control", description=__doc__.split("\n")[0])
    cli.add_argument("command", choices=("status", "stop", "reap"))
    cli.add_argument("--results-root", required=True, type=Path, help="the session's path")
    args = cli.parse_args(argv)
    lsf_dir = lsf_directory(args.results_root)
    if not (lsf_dir / "ledger.json").exists():
        out(f"{lsf_dir} has no ledger: was this session launched with lsf/submit.py?")
        return EXIT_PRECONDITION
    backend = backend or LsfBackend(
        diagnostics=lsf_dir / "logs" / read_json(lsf_dir / "launch.json")["run_id"] / "diagnostics"
    )
    return {"status": status, "stop": stop, "reap": reap}[args.command](lsf_dir, backend, out)


if __name__ == "__main__":
    sys.exit(main())
