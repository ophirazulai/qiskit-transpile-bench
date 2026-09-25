"""``clean``: delete a finished session's bulk and keep its results. It never touches the store.

Deleted: the evolved build, the source snapshots, the verifier environment and cache, worker
``scratch`` directories, ``oracle-jobs/``, the copied upstream test trees and the unit-test
stage's session-local ``CARGO_HOME`` directories, and verified
quality outputs and C6 prefixes larger than the policy's ``output_retention_bytes``.

Kept: ``run.json``, the profile, ``harness-wheel/``, ``stages/``, the verdict files and logs,
``observations.jsonl``, ``correctness.jsonl``, ``clifford.jsonl``, ``cost/``,
``changed-tests.json``, every ``job.json`` and the snapshot manifests. ``decide`` still works
afterwards; every other stage exits 41.

Every planned deletion is recorded in ``clean.json`` (a file's SHA-256, or a directory's
size and a digest of its listing) under ``status: cleaning`` before anything is deleted. A
killed ``clean`` resumes from that list.
"""

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from qtb.canonical import digest, read_json, write_json
from qtb.coordinator.stages import STAGE_ORDER, read_state, state_hash
from qtb.coordinator.storage import (
    LockBusy,
    locked,
    output_deletions,
    prefix_output_deletions,
    read_records,
)
from qtb.errors import Precondition


def _tree(path):
    files, size, listing = 0, 0, []
    for folder, _, names in os.walk(path):
        for name in sorted(names):
            item = Path(folder) / name
            try:
                stat = item.lstat()
            except OSError:
                continue
            files += 1
            size += stat.st_size
            listing.append([str(item.relative_to(path)), stat.st_size])
    return {"files": files, "bytes": size, "listing_sha256": digest(sorted(listing))}


def planned_deletions(root):
    """Everything ``clean`` deletes, with provenance recorded before deletion."""
    root = Path(root)
    directories = [root / "builds" / "evolved-build", root / "verifier", root / "verifier-cache"]
    directories += [root / "oracle-jobs"]
    directories += sorted(p for p in (root / "builds").glob("*") if p.is_dir())
    directories += sorted(root.glob("jobs/**/scratch"))
    for upstream in sorted(p for p in root.glob("upstream-*") if p.is_dir()):
        # The copied test tree, and the crates of a session-local CARGO_HOME.
        directories += [upstream / "test", upstream / "cargo"]
    planned = {}
    for path in directories:
        if path.is_dir() and str(path) not in planned:
            planned[str(path)] = dict(kind="directory", **_tree(path))
    policy = read_json(root / "policy.json")
    limit = policy["measurement_protocol"]["output_retention_bytes"]
    rows = read_records(root / "observations.jsonl")
    for path, provenance in {
        **output_deletions(root, rows, limit),
        **prefix_output_deletions(root, rows, limit),
    }.items():
        if not any(Path(path).is_relative_to(d) for d in directories):
            planned[path] = dict(kind="file", bytes=provenance["compressed_bytes"], **provenance)
    return planned


def clean(results_root, progress=print):
    """Returns the exit status: 0 when the session is clean, 41 when it is refused."""
    root = Path(results_root).resolve()
    if not (root / "run.json").exists():
        raise Precondition(f"{root} is not a session")
    try:
        with locked(root / "lifecycle.lock", wait=False):
            return _clean(root, progress)
    except LockBusy as exc:
        raise Precondition("A stage or decide is running on this session; clean later") from exc


def _clean(root, progress):
    path = root / "clean.json"
    state = read_json(path) if path.exists() else None
    if state and state["status"] == "complete":
        progress(f"Already clean: {root}")
        return 0
    if state is None:
        states = {stage: read_state(root, stage) for stage in STAGE_ORDER}
        running = [s for s, v in states.items() if v and v["status"] == "running"]
        if running:
            raise Precondition(
                f"{', '.join(running)} did not finish (a killed job leaves 'running'); "
                "run it again to a final state, then decide, then clean"
            )
        decision_file = root / "decision.json"
        if not decision_file.exists():
            raise Precondition("Run decide before clean")
        recorded = read_json(decision_file).get("stage_state_hashes", {})
        current = {stage: state_hash(root, stage) for stage in STAGE_ORDER}
        if recorded != current:
            changed = [s for s in STAGE_ORDER if recorded.get(s) != current[s]]
            raise Precondition(
                f"Stages changed since the last decide ({', '.join(changed)}); run decide again"
            )
        state = {
            "format": "qtb-clean/1",
            "status": "cleaning",
            "started_at": datetime.now(UTC).isoformat(),
            "stage_state_hashes": current,
            "planned": planned_deletions(root),
        }
        write_json(path, state)
    freed = 0
    for target, entry in state["planned"].items():
        target = Path(target)
        if entry["kind"] == "directory":
            if target.exists():
                shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
        freed += entry["bytes"]
    state.update(status="complete", finished_at=datetime.now(UTC).isoformat(), freed_bytes=freed)
    write_json(path, state)
    progress(f"Cleaned {root}: freed {freed / 2**30:.2f} GiB in {len(state['planned'])} items.")
    return 0
