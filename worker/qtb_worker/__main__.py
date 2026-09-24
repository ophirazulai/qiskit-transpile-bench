"""Versioned append-only JSONL worker protocol with one heartbeat per seed."""

import argparse
import os
import traceback
from pathlib import Path

from qtb.canonical import canonical_bytes, read_json
from qtb.config import PROTOCOL, validate
from qtb.errors import Unsupported
from qtb_worker.modes import load_inputs, run_seed, verify_provenance


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    job = validate("job", read_json(args.job))
    verify_provenance(job["build"])
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    inputs = load_inputs(job)
    with (output / "results.jsonl").open("ab", buffering=0) as stream:
        for seed in job["seeds"]:
            result = {"protocol": PROTOCOL, "mode": job["mode"], "seed": seed}
            try:
                result.update(run_seed(job, seed, output, inputs), status="ok")
            except Exception as exc:
                result.update(
                    status="unsupported" if isinstance(exc, Unsupported) else "error",
                    error=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc(),
                )
            stream.write(canonical_bytes(result) + b"\n")
            os.fsync(stream.fileno())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
