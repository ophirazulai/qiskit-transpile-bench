"""The baseline store: baseline builds and baseline results reused by many sessions.

It holds only baseline data. Nothing about an evolved tree, and no cost sample, is written
here. Every entry is content-addressed: a key covers everything its content depends on, so a
changed input gives a new key and a miss. Entries are published atomically, and several
sessions on several hosts may use one store at once::

    STORE/
      builds/<key>/          READY, build.json, env/, source/, cargo/, wheels/, build.log
      builds/<key>.lock      held while that key is being built
      wheels/<identity>/     baseline Qiskit wheels
      quality/<key>/         baseline quality observations and their output files
      correctness/<key>/     rows.jsonl, evidence.json, provenance.json
      unit-tests/<key>/      python.json, rust.json, logs, provenance.json
"""

import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from qtb.canonical import digest, read_json, write_json
from qtb.errors import HarnessError, Precondition, Usage

RESULT_KINDS = ("correctness", "unit-tests")
INVALIDATED = "invalidated.json"


def resolve_store(flag):
    """``--store`` wins over ``QTB_STORE``; there is no default and the root must exist."""
    value = flag or os.environ.get("QTB_STORE")
    if not value:
        raise Usage("compile needs --store DIR or QTB_STORE (there is no default)")
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise Usage(f"Store does not exist: {root} (create the directory first)")
    return root


def build_key(identity, harness_hash):
    """A stored build also contains the harness wheel, installed in its venv."""
    return digest({"identity": identity, "harness": harness_hash})


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def require(self):
        if not self.root.is_dir():
            raise Precondition(f"The session's store is missing: {self.root}")
        return self

    # Builds

    def build_dir(self, key):
        return self.root / "builds" / key

    def build_lock(self, key):
        return self.root / "builds" / f"{key}.lock"

    def ready_build(self, key):
        """The ready build under ``key``, or ``None``. Checks that it points only inside."""
        entry = self.build_dir(key)
        if not (entry / "READY").exists():
            return None
        build = read_json(entry / "build.json")
        for field in ("python", "environment", "wheel"):
            # A venv's interpreter is a symlink to its base Python: resolve only its directory.
            path = Path(build[field])
            if not (path.parent.resolve() / path.name).is_relative_to(entry):
                raise HarnessError(f"Stored build {key} points outside its entry: {field}")
        if Path(build["snapshot"]["path"]).resolve() != (entry / "source").resolve():
            raise HarnessError(f"Stored build {key} does not use its own source tree")
        return build

    @property
    def wheels(self):
        return self.root / "wheels"

    # Baseline quality observations (one directory per quality cache key)

    def quality_dir(self, key):
        return self.root / "quality" / key

    # Baseline correctness and unit-test results

    def results_dir(self, kind, key):
        if kind not in RESULT_KINDS:
            raise HarnessError(f"Unknown store result kind: {kind}")
        return self.root / kind / key

    def read_results(self, kind, key):
        """A published entry's directory, or ``None``. Publication is one atomic rename."""
        entry = self.results_dir(kind, key)
        if not (entry / "provenance.json").exists():
            return None
        return entry

    def publish_results(self, kind, key, fill, provenance):
        """Write an entry into a private directory, then rename it into place.

        ``fill(partial, final)`` writes the files into ``partial``; any path it records must
        name ``final``, where the files will live. If another session published the same key
        first, its entry is kept: both describe the same inputs.
        """
        final = self.results_dir(kind, key)
        if final.exists():
            return final
        partial = final.with_name(f".partial-{key}-{uuid.uuid4().hex[:8]}")
        partial.mkdir(parents=True)
        try:
            fill(partial, final)
            write_json(
                partial / "provenance.json",
                dict(provenance, key=key, first_computed=datetime.now(UTC).isoformat()),
            )
            try:
                os.rename(partial, final)
            except OSError:
                if not (final / "provenance.json").exists():
                    raise
        finally:
            if partial.exists():
                shutil.rmtree(partial, ignore_errors=True)
        return final

    def first_computed(self, kind, key):
        entry = self.read_results(kind, key)
        return read_json(entry / "provenance.json").get("first_computed") if entry else None
