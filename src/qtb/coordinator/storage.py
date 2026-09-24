"""Atomic run state, append-only observations, and scoped caches."""

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from qtb.canonical import canonical_bytes, digest, read_json, write_json
from qtb.config import PROTOCOL, case_hash
from qtb.envbuild import SERIAL
from qtb.errors import HarnessError


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def append_record(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path.with_suffix(path.suffix + ".lock")):
        with path.open("ab") as stream:
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
