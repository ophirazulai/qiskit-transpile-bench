"""Atomic run state, append-only observations, and scoped caches."""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from qtb.canonical import canonical_bytes, digest, read_json, write_json
from qtb.config import PROTOCOL, case_hash
from qtb.envbuild import SERIAL
from qtb.errors import HarnessError


def runner_lock():
    """One machine/user lock even when comparisons use different results roots."""
    return Path(tempfile.gettempdir()) / f"qtb-runner-{os.getuid()}.lock"


@contextmanager
def locked(path, shared=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def append_record(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path.with_suffix(path.suffix + ".lock")):
        with path.open("a+b") as stream:
            # A killed writer can leave an unterminated tail. Remove only that
            # tail before appending so it cannot corrupt the next complete row.
            end = stream.seek(0, os.SEEK_END)
            if end:
                stream.seek(end - 1)
                if stream.read(1) != b"\n":
                    cursor, boundary = end, 0
                    while cursor:
                        start = max(0, cursor - 65536)
                        stream.seek(start)
                        chunk = stream.read(cursor - start)
                        found = chunk.rfind(b"\n")
                        if found >= 0:
                            boundary = start + found + 1
                            break
                        cursor = start
                    stream.truncate(boundary)
            stream.write(canonical_bytes(row) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())


def read_records(path):
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    with path.open("rb") as stream:
        for line in stream:
            if not line.endswith(b"\n"):
                break  # interrupted last write is never accepted as an observation
            try:
                rows.append(json.loads(line))
            except ValueError as exc:
                raise HarnessError(f"Corrupt completed record in {path}") from exc
    return rows


def quality_cache_key(
    build,
    case,
    machine,
    measurement_protocol,
    harness_hash,
    block="B0",
    mode="quality",
    hash_seed="0",
):
    if mode not in {"quality", "prefix", "roundtrip"}:
        raise HarnessError("Cost observations cannot enter the revision cache")
    return digest(
        {
            "build": build["id"],
            "case": case_hash(case),
            "machine": machine,
            "protocol": PROTOCOL,
            "harness": harness_hash,
            "block": block,
            "mode": mode,
            "measurement_protocol": measurement_protocol,
            "worker_environment": dict(SERIAL, PYTHONHASHSEED=hash_seed),
        }
    )


def register_decision(root, manifest_hash, run_id):
    path = Path(root) / "decision-counts.json"
    with locked(Path(root) / "decision-counts.lock"):
        state = read_json(path) if path.exists() else {}
        runs = state.setdefault(manifest_hash, [])
        if run_id in runs:
            return runs.index(run_id)
        before = len(runs)
        runs.append(run_id)
        write_json(path, state)
        return before


def prune_outputs(directory, observations, limit):
    """Retain failing/non-verified outputs; record every successful large-output deletion."""
    directory = Path(directory).resolve()
    path = directory / "retention.json"
    pruned = read_json(path) if path.exists() else {}
    for row in observations:
        if not row.get("output") or not row.get("checks"):
            continue
        if any(check["status"] != "verified" for check in row["checks"]):
            continue
        output = Path(row["output"]).resolve()
        if output.is_relative_to(directory) and output.exists() and output.stat().st_size > limit:
            pruned[str(output)] = {
                "output_hash": row["output_hash"],
                "observation_id": row["id"],
                "compressed_bytes": output.stat().st_size,
            }
            # Persist the reason and hash before deletion, preserving replay.
            write_json(path, pruned)
            output.unlink()
    return pruned
