"""The scheduler-independent execution context of one stage invocation.

The harness never reads a scheduler's environment. An entry point that runs a stage under a
scheduler (``lsf/job.py``) translates its allocation into an ``Execution`` and installs it
around the stage; a direct command-line run uses the default, empty context.

- ``scheduler``: recorded as the stage state's ``scheduler`` field (job identity, host, slots).
- ``workers``: parallel workers the allocation grants; ``None`` keeps the local default.
- ``invocation``: identity of this invocation, recorded in the stage state and its history.
- ``session``: fields that ``compile`` records in a new session's ``run.json``, such as
  ``cost_evidence``, the extension every cost bundle of the session must satisfy.
- ``cost_monitor``: the measurement extension of the cost stage. It checks the host before
  the stage runs (``prepare``), observes every cost worker through ``worker_hooks`` and adds
  its evidence to each bundle (``begin_bundle``/``end_bundle``). See ``qtb.coordinator.costs``.
- ``cleanup``: carries out ``decide``'s follow-up cleanup; ``None`` cleans in this process.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Execution:
    scheduler: dict = field(default_factory=dict)
    workers: int | None = None
    invocation: dict | None = None
    session: dict = field(default_factory=dict)
    cost_monitor: object | None = None
    cleanup: object | None = None


_current = Execution()


def current():
    return _current


@contextmanager
def installed(execution):
    """Run the enclosed stage under ``execution``."""
    global _current
    previous, _current = _current, execution
    try:
        yield execution
    finally:
        _current = previous
