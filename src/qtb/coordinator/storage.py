"""Atomic session state, append-only observations, locks and cache helpers."""

import fcntl
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from qtb.canonical import canonical_bytes, digest, file_hash, read_json, write_json
from qtb.config import PROTOCOL
from qtb.envbuild import SERIAL
from qtb.errors import HarnessError

# Measurement-protocol settings that only shape cost sessions.
COST_PROTOCOL_KEYS = {
    "warmups",
    "minimum_calls",
    "minimum_ns",
    "screen_rounds",
    "timing_rounds",
    "companion_rounds",
    "companion_seeds",
    "memory_processes",
    "rerun_multiplier",
}


class LockBusy(HarnessError):
    """A non-blocking lock is held by another process."""


def runner_lock():
    """One machine/user lock even when comparisons use different results roots.

    The path is fixed under ``/tmp``: LSF often sets a per-job ``TMPDIR``, which would make a
    lock under ``tempfile.gettempdir()`` private to one job. ``QTB_RUNNER_LOCK`` overrides it.
    """
    return Path(os.environ.get("QTB_RUNNER_LOCK") or f"/tmp/qtb-runner-{os.getuid()}.lock")


@contextmanager
def locked(path, shared=False, wait=True):
    """``flock`` on ``path``; with ``wait=False`` a held lock raises ``LockBusy``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(stream, mode if wait else mode | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusy(f"Lock is held by another process: {path}") from exc
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
    # A cached observation includes checks, so fields that choose those checks
    # (for example role and constraint form) remain part of its identity.
    case_definition = {
        k: v
        for k, v in case.items()
        if k
        not in {
            "case_id", "weight", "panel", "family", "size_band", "provenance",
            "seeds_per_block", "timeout_s", "topology", "input_group", "modes",
            "clifford_variant", "native_basis", "variant", "active_qubits",
        }
    }
    # Cost-only protocol settings (rounds, warm-ups, minimum calls) never touch a
    # quality compile, so changing them must not discard cached observations.
    quality_protocol = {
        k: v for k, v in measurement_protocol.items() if k not in COST_PROTOCOL_KEYS
    }
    return digest(
        {
            "build": build["id"],
            "case": digest(case_definition),
            "machine_cpu": machine.get("cpu"),
            "protocol": PROTOCOL,
            "harness": harness_hash,
            "block": block,
            "mode": mode,
            "measurement_protocol": quality_protocol,
            "worker_environment": dict(SERIAL, PYTHONHASHSEED=hash_seed),
        }
    )


def cache_quality_observation(cache, seed, observation):
    """Copy an observation's artifacts into a store entry, outside any session."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    cached = dict(observation)
    cached["worker"] = dict(observation["worker"])
    cached["checks"] = deepcopy(observation.get("checks", []))

    def copy_artifact(source, destination):
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as temp:
            temp_path = Path(temp.name)
            try:
                with Path(source).open("rb") as stream:
                    shutil.copyfileobj(stream, temp)
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise
        os.replace(temp_path, destination)

    for field, source in (
        ("output", observation["output"]),
        ("job_file", observation["worker"]["job_file"]),
    ):
        source = Path(source)
        destination = cache / f"{seed}.{source.name}"
        copy_artifact(source, destination)
        if field == "output":
            cached[field] = str(destination)
            cached["worker"][field] = str(destination)
            cached["cache_output_sha256"] = file_hash(destination)
        else:
            cached["worker"][field] = str(destination)
    for check in cached["checks"]:
        if check.get("oracle") != "C6":
            continue
        prefix_jobs = []
        for index, prefix in enumerate(check.get("prefix_outputs", [])):
            if not prefix.get("job_file"):
                continue
            destination = cache / f"{seed}.prefix-{index}.job.json"
            copy_artifact(prefix["job_file"], destination)
            prefix["job_file"] = str(destination)
            prefix["output"] = None
            prefix["output_retained"] = False
            prefix_jobs.append(str(destination))
        if prefix_jobs:
            check["prefix_jobs"] = prefix_jobs
    write_json(cache / f"{seed}.json", cached)


def cached_quality_observation(path):
    """Reject stale or damaged cached outputs rather than reusing their checks."""
    cached = read_json(path)
    output = Path(cached["output"])
    if output.exists() and cached.get("cache_output_sha256") == file_hash(output):
        return cached
    return None


QUALITY_INVALIDATED = "invalidated.json"


def quality_entry_valid(entry):
    """A quality entry invalidated by a failed determinism audit is never reused."""
    return not (Path(entry) / QUALITY_INVALIDATED).exists()


def invalidate_quality_entry(entry, reason):
    """Mark an entry unusable under its lock; its files stay, so readers never lose them."""
    entry = Path(entry)
    if not entry.exists():
        return
    with locked(entry / ".lock"):
        if quality_entry_valid(entry):
            write_json(entry / QUALITY_INVALIDATED, {"reason": reason})


def store_quality_observation(entry, seed, observation):
    """Add one baseline observation to a valid entry; returns whether it was stored."""
    entry = Path(entry)
    with locked(entry / ".lock", shared=True):
        if not quality_entry_valid(entry):
            return False
        cache_quality_observation(entry, seed, observation)
    return True


def output_deletions(directory, observations, limit):
    """Plan deletion of large outputs whose checks all verified; failures stay for review.

    Returns ``{path: provenance}``. Only files inside ``directory`` are planned, so outputs
    replayed from the baseline store are never touched. Nothing is deleted here.
    """
    directory = Path(directory).resolve()
    planned = {}
    for row in observations:
        if not row.get("output") or not row.get("checks"):
            continue
        if any(check["status"] != "verified" for check in row["checks"]):
            continue
        output = Path(row["output"]).resolve()
        if output.is_relative_to(directory) and output.exists() and output.stat().st_size > limit:
            planned[str(output)] = {
                "output_hash": row["output_hash"],
                "observation_id": row["id"],
                "compressed_sha256": file_hash(output),
                "compressed_bytes": output.stat().st_size,
            }
    return planned


def prefix_output_deletions(directory, observations, limit):
    """Plan deletion of verified C6 prefixes whose hash and job survive in the evidence."""
    directory = Path(directory).resolve()
    planned = {}
    for row in observations:
        for check in row.get("checks", []):
            if check.get("oracle") != "C6" or check.get("status") != "verified":
                continue
            for prefix in check.get("prefix_outputs", []):
                if not all(prefix.get(key) for key in ("output", "output_hash", "job_file")):
                    continue
                output = Path(prefix["output"]).resolve()
                job_file = Path(prefix["job_file"]).resolve()
                if (
                    not output.is_relative_to(directory)
                    or not job_file.is_relative_to(directory)
                    or output.parent.parent != job_file.parent
                    or not output.exists()
                    or not job_file.exists()
                    or output.stat().st_size <= limit
                ):
                    continue
                job = read_json(job_file)
                if job.get("mode") != "prefix" or row.get("seed") not in job.get("seeds", []):
                    continue
                # The archived C6 check and job identify the verification and compile.
                planned[str(output)] = {
                    "oracle": "C6",
                    "stage": prefix["stage"],
                    "output_hash": prefix["output_hash"],
                    "compressed_sha256": file_hash(output),
                    "observation_id": row["id"],
                    "job_file": str(job_file),
                    "compressed_bytes": output.stat().st_size,
                }
    return planned
