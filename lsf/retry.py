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

A retry avoids hosts that were noisy before (``excluded_hosts``): a host whose idle probe saw
foreign activity at once, one whose measurement was contaminated after a clean idle probe
after ``HOST_NOISY_LIMIT`` such attempts. At most ``MAX_EXCLUDED_HOSTS``, the most recently
noisy, are excluded, so the tier's pool is never exhausted by exclusions alone.

A resume (``lsf/submit.py --resume``) starts a new epoch (``begin_epoch``): the previous
epoch's cost accounting, verdict, cleanup and result are archived in ``resumes``, and the
new epoch starts with a fresh budget, no excluded hosts and every unfinished stage allowed
one new job. Every job entry records its epoch; only cancelling and reconciling look at
all of them. Attempt numbers keep counting across epochs.
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
# Noisy measurements on one host (after a clean idle probe) before retries avoid it.
HOST_NOISY_LIMIT = 2
MAX_EXCLUDED_HOSTS = 8
ACTIVE = {"reserved", "submitted", "ambiguous"}
CONSUMING = {"reserved", "submitted", "ambiguous", "unreconciled", "terminal"}


# What one epoch owns; a resume archives these and starts over.
EPOCH_KEYS = ("cost", "decide", "cleanup", "result")


def now():
    return datetime.now(UTC).isoformat()


def fresh_epoch():
    return {
        "cost": {"max_retries": MAX_COST_RETRIES, "exhausted": None, "stopped": None},
        "decide": None,
        "cleanup": None,
        "result": None,
    }


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
            **fresh_epoch(),
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

    # Epochs

    @property
    def epoch(self):
        """The current epoch: 0 until the first resume."""
        return len(self.data.get("resumes") or [])

    def begin_epoch(self, **record):
        """Archive the current epoch's accounting and start the next; returns its record."""
        resumes = self.data.setdefault("resumes", [])
        entry = {
            "index": len(resumes) + 1,
            "at": now(),
            "user": getpass.getuser(),
            **record,
            "previous": {key: self.data.get(key) for key in EPOCH_KEYS},
        }
        resumes.append(entry)
        self.data.update(fresh_epoch())
        self.save()
        return entry

    # Jobs

    def jobs(self, stage=None):
        entries = self.data["jobs"].values()
        return [e for e in entries if stage is None or e["stage"] == stage]

    def current(self, stage=None):
        """The stage's entries of the current epoch."""
        return [e for e in self.jobs(stage) if e.get("epoch", 0) == self.epoch]

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
            "epoch": self.epoch,
            **fields,
        }
        self.save()
        return self.data["jobs"][key]

    def update(self, key, **fields):
        self.data["jobs"][key].update(fields)
        self.save()
        return self.data["jobs"][key]

    def latest(self, stage):
        """The stage's most recent entry of this epoch that was not positively rejected."""
        entries = [e for e in self.current(stage) if e["status"] != "rejected"]
        return max(entries, key=lambda e: (e.get("attempt") or 0, e["reserved_at"]), default=None)

    def active(self):
        return [e for e in self.jobs() if e["status"] in ACTIVE]

    # Cost attempts

    def cost_attempts(self):
        """This epoch's cost attempts."""
        return sorted(
            (e for e in self.current("cost") if e["status"] != "rejected"),
            key=lambda e: e["attempt"],
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


def excluded_hosts(ledger):
    """Hosts the next cost job must avoid, the most recently noisy last."""
    strikes, excluded = {}, []
    for entry in ledger.cost_attempts():
        outcome = entry.get("outcome") or {}
        host = (entry.get("scheduler") or {}).get("host") or outcome.get("host")
        if outcome.get("kind") != "noisy" or not host:
            continue
        idle = (outcome.get("contamination") or {}).get("phase") == "idle probe"
        strikes[host] = strikes.get(host, 0) + (HOST_NOISY_LIMIT if idle else 1)
        if strikes[host] >= HOST_NOISY_LIMIT:
            if host in excluded:
                excluded.remove(host)
            excluded.append(host)
    return excluded[-MAX_EXCLUDED_HOSTS:]


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
