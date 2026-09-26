"""Re-pin an unfinished cost stage to the running harness: ``run.json:amendments``.

A session runs every stage with the harness that compiled it (``Comparison.open``). When
that harness cannot finish the cost stage, for example because its monitor aborts on noise
a later harness no longer judges, ``amend_cost`` lets the cost stage alone continue under
the running harness. It is refused unless:

- ``compile``, ``quality`` and ``correctness`` are final (``unit-tests`` is final or never
  started), so no stage but cost runs under the new harness;
- ``cost`` has no valid result: it is not started, ``running`` (killed), ``failed`` or
  ``noisy``. A final cost stage is never measured again, under any harness;
- the session is not cleaned, no stage or decide holds the session, and the profile's
  manifest and policy are unchanged.

The amendment is appended to ``run.json:amendments`` and the file is tagged ``qtb-run/4``;
nothing else in ``run.json`` changes.
It records the harness identities the cost stage now requires (``hashes``), the session
requirements it replaces (``session``, for example ``cost_evidence``), the ones it replaced
(``previous``), the reason, and the archived wheel of the new harness
(``harness-wheel/amendments/<n>/``). ``effective`` applies the amendments: the ``session``
requirements for every reader, the ``hashes`` only to the stage they name. Saved cost
bundles are not moved: each is checked against the amended requirement before reuse, and
measured again when it fails. ``decide`` notes every amendment.
"""

import copy
from datetime import UTC, datetime
from pathlib import Path

# run.json gains ``amendments``: an amended session is tagged with the next run format.
FORMAT = "qtb-run/4"
AMENDABLE = "cost"
HASHES = ("coordinator", "implementation")
# Stages that must be final before cost may be re-pinned; unit-tests may also be absent.
BEFORE_COST = ("compile", "quality", "correctness")


def effective(run, stage=None):
    """``run`` with its amendments applied: requirements always, hashes for ``stage``."""
    amendments = run.get("amendments") or []
    if not amendments:
        return run
    view = copy.deepcopy(run)
    for amendment in amendments:
        view.update(copy.deepcopy(amendment.get("session") or {}))
        if stage is not None and amendment["stage"] == stage:
            view["hashes"].update(amendment["hashes"])
    return view


def notes(run):
    """One ``decide`` note per amendment."""
    return [
        f"Amendment {a['index']} ({a['at'][:10]}): {a['stage']} was measured by a later "
        f"harness than the one that compiled ("
        f"{', '.join(['harness identity', *sorted(a.get('session') or {})])} re-pinned; "
        f"reason: {a['reason']})."
        for a in run.get("amendments") or []
    ]


def amend_cost(results_root, session, reason, progress=print):
    """Re-pin the unfinished cost stage to the running harness; returns the amendment.

    ``session`` holds the requirements the running harness's execution context sets for a
    new session (``execution.current().session``); they replace the recorded ones.
    """
    from qtb.coordinator.storage import LockBusy, locked
    from qtb.errors import Precondition, Usage

    if not reason or not reason.strip():
        raise Usage("An amendment needs a reason")
    root = Path(results_root).resolve()
    if not (root / "run.json").exists():
        raise Precondition(f"{root} is not a session")
    try:
        with locked(root / "lifecycle.lock", wait=False):
            return _amend(root, session, reason.strip(), progress)
    except LockBusy as exc:
        raise Precondition("A stage or decide is running on this session; amend later") from exc


def _amend(root, session, reason, progress):
    from qtb.canonical import file_hash, read_json, write_json
    from qtb.config import coordinator_identity, implementation_identity, load_profile
    from qtb.coordinator import build_harness_wheel
    from qtb.coordinator.stages import FINAL, read_state, refuse_cleaned, stage_path
    from qtb.errors import Precondition

    refuse_cleaned(root)
    run = read_json(root / "run.json")
    for stage in BEFORE_COST:
        state = read_state(root, stage)
        if not state or state["status"] not in FINAL:
            status = state["status"] if state else "not started"
            raise Precondition(f"Only cost can be re-pinned: {stage} is {status}, not final")
    tests = read_state(root, "unit-tests")
    if tests and tests["status"] not in FINAL:
        raise Precondition(
            f"Only cost can be re-pinned: unit-tests is {tests['status']}, not final"
        )
    cost = read_state(root, AMENDABLE)
    if cost and cost["status"] in FINAL:
        raise Precondition(
            f"cost is already {cost['status']}: a final measurement is never measured again, "
            "under any harness"
        )
    _, _, hashes = load_profile(run["profile"], verify=False)
    for name in ("manifest", "policy"):
        if hashes[name] != run["hashes"][name]:
            raise Precondition(f"The profile's {name} changed since compile")
    current = effective(run, AMENDABLE)
    wanted = {"coordinator": coordinator_identity(), "implementation": implementation_identity()}
    same = all(current["hashes"].get(k) == v for k, v in wanted.items())
    if same and all(current.get(k) == v for k, v in session.items()):
        raise Precondition("cost already runs under this harness; nothing to amend")
    amendments = list(run.get("amendments") or [])
    index = len(amendments) + 1
    wheels = root / "harness-wheel" / "amendments" / str(index)
    progress(f"Archiving the running harness in {wheels}")
    wheel = build_harness_wheel(wheels, root, root / f"harness-build-amendment-{index}.log")
    amendment = {
        "index": index,
        "at": datetime.now(UTC).isoformat(),
        "stage": AMENDABLE,
        "reason": reason,
        "hashes": dict(wanted, harness_wheel=file_hash(wheel)),
        "session": dict(session),
        "previous": {
            "hashes": {k: current["hashes"].get(k) for k in HASHES},
            "session": {k: current.get(k) for k in session},
            "cost_status": cost["status"] if cost else None,
            "format": run.get("format"),
        },
        "wheel": wheel.relative_to(root).as_posix(),
    }
    run["amendments"] = amendments + [amendment]
    run["format"] = FORMAT
    # The one writer of run.json besides compile, and it only appends an amendment.
    write_json(root / "run.json", run)
    record = stage_path(root, AMENDABLE, f"amendment-{index}.json")
    record.parent.mkdir(parents=True, exist_ok=True)
    write_json(record, amendment)
    progress(f"cost re-pinned to the running harness (amendment {index}): {root}")
    return amendment
