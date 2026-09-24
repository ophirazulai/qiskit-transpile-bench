"""Subprocess scheduler with per-compile heartbeats and clean import paths."""

import os
import signal
import subprocess
import time
from pathlib import Path

from qtb.canonical import write_json
from qtb.config import PROTOCOL, validate
from qtb.coordinator.storage import read_records
from qtb.envbuild import sanitized_environment
from qtb.errors import HarnessError


def run_worker(build, job, directory, hash_seed="0"):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    # The full source inventory belongs to run/build metadata, not every seed
    # job. Workers need only the immutable identity and import provenance.
    worker_build = {
        k: build[k]
        for k in ("id", "python", "environment", "provenance", "wheel", "wheel_sha256")
        if k in build
    }
    job = dict(job, protocol=PROTOCOL, build=worker_build)
    validate("job", job)
    write_json(directory / "job.json", job)
    scratch = directory / "scratch"
    scratch.mkdir()
    result_dir = directory / "out"
    result_dir.mkdir()
    command = [
        build["python"],
        "-P",
        "-m",
        "qtb_worker",
        "--job",
        str(directory / "job.json"),
        "--out",
        str(result_dir),
    ]
    timeout, last, completed = job["timeout_s"], time.monotonic(), 0
    with (directory / "worker.log").open("wb") as log:
        process = subprocess.Popen(
            command,
            cwd=scratch,
            env=sanitized_environment({"PYTHONHASHSEED": hash_seed}),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            while process.poll() is None:
                rows = read_records(result_dir / "results.jsonl")
                if len(rows) > completed:
                    completed, last = len(rows), time.monotonic()
                if time.monotonic() - last > timeout:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    break
                time.sleep(0.1)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    results = read_records(result_dir / "results.jsonl")
    expected = set(job["seeds"])
    seen = set()
    for row in results:
        validate("result", row)
        if row["seed"] not in expected or row["seed"] in seen or row["mode"] != job["mode"]:
            raise HarnessError("Worker returned duplicate/unrequested results")
        seen.add(row["seed"])
    if seen != expected:
        pending = [seed for seed in job["seeds"] if seed not in seen]
        results.append(
            {
                "protocol": PROTOCOL,
                "status": "error",
                "mode": job["mode"],
                "seed": pending[0],
                "error": "Worker timeout or unexpected exit",
                "returncode": process.returncode,
                "log": str(directory / "worker.log"),
            }
        )
        # A completed seed is durable; unattempted seeds can be rescheduled on resume.
    return results
