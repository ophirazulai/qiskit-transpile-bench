"""The durable orchestration ledger and the cost noise-retry budget.

The ledger (``<session>.lsf/ledger.json``) is the sole authority for noise-retry
accounting; the harness's own invocation counts are never used for it. Every job the
manager submits gets an entry *before* ``bsub`` runs, with a unique nonce-bearing name, and
the entry moves through::

    reserved -> submitted -> terminal      (accepted; its job ID is recorded)
    reserved -> rejected                   (bsub positively refused it)
    reserved -> ambiguous -> submitted | unreconciled
                                           (no clear answer; found again by name)

Cost attempts: one initial job plus at most ``MAX_COST_RETRIES`` retries. An accepted job
consumes its attempt even when it was killed before reporting. A rejected submission does
not; an ambiguous one does. An ambiguous submission blocks further attempts while it is
looked up by name; if it is never found (``unreconciled``) it still counts, it is still
looked up and cancelled if it appears, and, having no outcome, it allows no retry. A job
that only recorded ``skipped`` consumes nothing. Only a job that ended ``noisy`` (positive
contamination evidence) allows another attempt; a killed, failed or lost one does not. The
ledger is rewritten atomically, and only under the session's orchestration lock.
"""

import getpass
from datetime import UTC, datetime
from pathlib import Path

from qtb.canonical import read_json, write_json

from lsf import logging as log

FORMAT = "qtb-lsf-ledger/1"
MAX_COST_RETRIES = 20
MAX_COST_ATTEMPTS = MAX_COST_RETRIES + 1
# Consecutive positively rejected submissions of one job before the manager gives up.
MAX_REJECTIONS = 3
ACTIVE = {"reserved", "submitted", "ambiguous"}
CONSUMING = {"reserved", "submitted", "ambiguous", "unreconciled", "terminal"}


def now():
    return datetime.now(UTC).isoformat()


class Ledger:
    def __init__(self, path, data):
        self.path = Path(path)
        self.data = data

    @classmethod
    def create(cls, path, *, run_id, session, lsf_dir):
        data = {
            "format": FORMAT,
            "run_id": run_id,
            "session": str(session),
            "lsf_dir": str(lsf_dir),
            "user": getpass.getuser(),
            "created_at": now(),
            "managers": [],
            "jobs": {},
            "cost": {"max_retries": MAX_COST_RETRIES, "exhausted": None, "stopped": None},
            "decide": None,
            "cleanup": None,
            "result": None,
        }
        ledger = cls(path, data)
        ledger.save()
        return ledger

    @classmethod
    def load(cls, path):
        data = read_json(path)
        if data.get("format") != FORMAT:
            raise ValueError(f"Unknown ledger format {data.get('format')} in {path}")
        return cls(path, data)

    def save(self):
        write_json(self.path, self.data)

    # Jobs

    def jobs(self, stage=None):
        entries = self.data["jobs"].values()
        return [e for e in entries if stage is None or e["stage"] == stage]

    def job(self, key):
        return self.data["jobs"][key]

    def reserve(self, key, **fields):
        """Record the intent to submit before ``bsub`` runs."""
        if key in self.data["jobs"]:
            raise ValueError(f"Job {key} is already in the ledger")
        self.data["jobs"][key] = {
            "key": key,
            "status": "reserved",
            "job_id": None,
            "reserved_at": now(),
            "submissions": [],
            "outcome": None,
            "consumes_budget": None,
            **fields,
        }
        self.save()
        return self.data["jobs"][key]

    def update(self, key, **fields):
        self.data["jobs"][key].update(fields)
        self.save()
        return self.data["jobs"][key]

    def latest(self, stage):
        """The stage's most recent entry that was not positively rejected."""
        entries = [e for e in self.jobs(stage) if e["status"] != "rejected"]
        return max(entries, key=lambda e: (e.get("attempt") or 0, e["reserved_at"]), default=None)

    def active(self):
        return [e for e in self.jobs() if e["status"] in ACTIVE]

    # Cost attempts

    def cost_attempts(self):
        return sorted(
            (e for e in self.jobs("cost") if e["status"] != "rejected"), key=lambda e: e["attempt"]
        )

    def next_attempt(self):
        return max((e["attempt"] for e in self.jobs("cost")), default=0) + 1


def budget_used(ledger):
    return sum(
        1
        for entry in ledger.cost_attempts()
        if entry["status"] in CONSUMING and entry.get("consumes_budget") is not False
    )


def decision(ledger):
    """Whether another cost job may be submitted, and why; logged by the caller."""
    attempts = ledger.cost_attempts()
    used = budget_used(ledger)
    result = {
        "used": used,
        "remaining": max(0, MAX_COST_ATTEMPTS - used),
        "limit": MAX_COST_ATTEMPTS,
        "next_attempt": ledger.next_attempt(),
        "exhausted": False,
    }
    last = attempts[-1] if attempts else None
    if last is not None and last["status"] in ACTIVE:
        return dict(result, allowed=False, reason=f"attempt {last['attempt']} is unsettled")
    outcome = (last or {}).get("outcome") or {}
    if last is not None and outcome.get("kind") != "noisy":
        return dict(
            result,
            allowed=False,
            reason=(
                f"attempt {last['attempt']} ended {outcome.get('kind', last['status'])}; "
                "only positive contamination evidence allows a noise retry"
            ),
        )
    if used >= MAX_COST_ATTEMPTS:
        return dict(
            result,
            allowed=False,
            exhausted=True,
            reason=f"the retry budget is exhausted ({used} of {MAX_COST_ATTEMPTS} cost jobs)",
        )
    reason = "initial cost job" if last is None else f"attempt {last['attempt']} was noisy"
    return dict(result, allowed=True, reason=reason)


def record_exhaustion(ledger, reason):
    """Finalize exhaustion in the ledger only: no stage job, no harness state is touched."""
    if ledger.data["cost"].get("exhausted"):
        return
    ledger.data["cost"]["exhausted"] = {"at": now(), "reason": reason, "used": budget_used(ledger)}
    ledger.save()
    log.warning("retry.exhausted", reason, used=budget_used(ledger), limit=MAX_COST_ATTEMPTS)


def record_stop(ledger, reason):
    ledger.data["cost"]["stopped"] = {"at": now(), "reason": reason}
    ledger.save()
