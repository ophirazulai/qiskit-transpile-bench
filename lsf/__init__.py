"""Running benchmark sessions on an LSF cluster, with a monitored quiet cost job.

The harness (``qtb``) executes one stage invocation and reports its outcome. Everything
specific to LSF lives here:

======================  =================================================================
``submit.py``           launcher: validates the configuration, submits the manager job
``manager.py``          manager job: stage order, concurrency, cost retries, inline decide,
                        automatic cleanup job, deadline, cancellation and recovery
``job.py``              entry point of every stage job: translates the allocation into an
                        execution context, installs the cost monitor, writes an outcome
``control.py``          ``status``, ``stop`` and ``reap`` for a session's jobs
``context.py``          allocation adapter: LSF environment to a validated execution context
``cost_monitor.py``     quiet-core placement, idle probe, counters, checks A and B
``cost_evidence.py``    validates monitoring evidence for reuse, admission and replay
``retry.py``            durable ledger, noise-retry accounting, the retry cap, exhaustion
``scheduler.py``        ``bsub``/``bjobs``/``bhist``/``bkill`` and resource requests
``report.py``           the companion orchestration report
``logging.py``          readable logs and JSONL events for every module here
======================  =================================================================

``context.py``, ``cost_monitor.py`` and ``cost_evidence.py`` decide whether a measurement
counts: they are part of the harness identity that a session pins.
"""

from pathlib import Path

from qtb.canonical import digest, file_hash

# The modules whose code decides whether cost evidence is admissible.
MEASUREMENT_MODULES = ("context.py", "cost_monitor.py", "cost_evidence.py")


def measurement_identity():
    """Content identity of the measurement-facing modules, recorded with every bundle."""
    root = Path(__file__).resolve().parent
    return digest({name: file_hash(root / name) for name in MEASUREMENT_MODULES})


def lsf_directory(session):
    """Orchestration records of a session: a sibling of it, outside its cleanup scope."""
    session = Path(session).resolve()
    return session.with_name(session.name + ".lsf")
