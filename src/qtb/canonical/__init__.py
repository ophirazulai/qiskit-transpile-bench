"""Versioned, deterministic, Qiskit-independent artifact IO."""

import gzip
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

from qtb.errors import HarnessError

CIRCUIT_FORMAT = "qtb-circuit/1"
TARGET_FORMAT = "qtb-target/1"


def canonical_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def number(value):
    value = float(value)
    if not math.isfinite(value):
        raise HarnessError("Non-finite canonical number")
    return value.hex()


def numeric(value):
    result = float.fromhex(value) if isinstance(value, str) else float(value)
    if not math.isfinite(result):
        raise HarnessError("Non-finite canonical number")
    return result


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, value):
    atomic_bytes(path, canonical_bytes(value) + b"\n")


def read_json(path):
    def reject(value):
        raise HarnessError(f"Invalid JSON number: {value}")

    try:
        return json.loads(Path(path).read_text(), parse_constant=reject)
    except (OSError, ValueError) as exc:
        raise HarnessError(f"Cannot read {path}: {exc}") from exc


def safe_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise HarnessError(f"Artifact escapes its root: {relative}")
    return path


def write_circuit(path, header, operations):
    if header.get("format") != CIRCUIT_FORMAT:
        raise HarnessError("Unknown circuit format")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    h = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as raw:
            with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as stream:
                for item in _items(header, operations):
                    line = canonical_bytes(item) + b"\n"
                    h.update(line)
                    stream.write(line)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return h.hexdigest()


def _items(header, operations):
    yield header
    yield from operations


def circuit_lines(path):
    with gzip.open(path, "rb") as stream:
        for line in stream:
            value = json.loads(line)
            if canonical_bytes(value) + b"\n" != line:
                raise HarnessError(f"Non-canonical circuit encoding: {path}")
            yield value


def read_circuit(path):
    lines = circuit_lines(path)
    try:
        header = next(lines)
    except StopIteration as exc:
        raise HarnessError("Empty circuit artifact") from exc
    if not isinstance(header, dict) or header.get("format") != CIRCUIT_FORMAT:
        raise HarnessError("Unknown circuit format")
    return header, list(lines)


def circuit_hash(path):
    h = hashlib.sha256()
    for item in circuit_lines(path):
        h.update(canonical_bytes(item) + b"\n")
    return h.hexdigest()


def verify_artifact(root, artifact, circuit=False):
    path = safe_path(root, artifact["file"])
    actual = circuit_hash(path) if circuit else digest(read_json(path))
    if actual != artifact["sha256"]:
        raise HarnessError(f"Artifact hash mismatch: {path}")
    return path
