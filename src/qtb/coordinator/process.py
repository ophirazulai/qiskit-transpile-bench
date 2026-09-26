"""Subprocess scheduler with per-compile heartbeats and clean import paths.

``run_worker`` accepts lifecycle ``hooks`` through which a measurement extension observes
every actual worker process, including the fresh process started after a per-entry timeout:

- ``spawn_options()``: extra ``Popen`` arguments, for example CPU placement;
- ``launched(pid)``: the process has started;
- ``progress(pid, completed)``: called at every poll with the number of heartbeat rows;
  returning an exception stops and reaps the worker, then raises it (abort before commit);
- ``exited(pid)``: the process has ended but is not reaped yet, so its counters are readable;
- ``reaped(pid, returncode)``: the process is gone; returning an exception raises it.
- optional ``measurement(pid, event)``: acknowledged ``b"B"``/``b"E"`` boundaries
  around each measured entry, excluding setup and warmup; may return an abort exception.

An exception returned by a hook ends the whole call: later entries are not retried.
"""

import os
import select
import signal
import socket
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path

from qtb.canonical import write_json
from qtb.config import PROTOCOL, validate
from qtb.coordinator.storage import read_records
from qtb.envbuild import sanitized_environment
from qtb.errors import HarnessError


def _has_exited(process, hooks):
    """Whether the worker ended, without reaping it when hooks want its final counters."""
    if hooks is None or not hasattr(os, "waitid"):
        return process.poll() is not None
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    return os.waitid(os.P_PID, process.pid, flags) is not None


def _await_exit(process):
    if hasattr(os, "waitid"):
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
    else:
        process.wait()


def run_worker(build, job, directory, hash_seed="0", hooks=None):
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
    timed_out, abort = False, None
    with ExitStack() as stack:
        log = stack.enter_context((directory / "worker.log").open("wb"))
        environment = {"PYTHONHASHSEED": hash_seed, "PYTHONDONTWRITEBYTECODE": "1"}
        options = hooks.spawn_options() if hooks else {}
        observer = peer = None
        if hooks and hasattr(hooks, "measurement"):
            observer, peer = socket.socketpair()
            stack.enter_context(observer)
            stack.enter_context(peer)
            environment["QTB_MEASUREMENT_FD"] = str(peer.fileno())
            options["pass_fds"] = (*options.get("pass_fds", ()), peer.fileno())
        process = subprocess.Popen(
            command,
            cwd=scratch,
            # A stored baseline build is shared and immutable: never write bytecode into it.
            env=sanitized_environment(environment),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            **options,
        )
        if peer is not None:
            peer.close()
        try:
            if hooks:
                hooks.launched(process.pid)
            while not _has_exited(process, hooks):
                rows = read_records(result_dir / "results.jsonl")
                if len(rows) > completed:
                    completed, last = len(rows), time.monotonic()
                if hooks:
                    abort = hooks.progress(process.pid, completed)
                    if abort is not None:
                        os.killpg(process.pid, signal.SIGKILL)
                        break
                if time.monotonic() - last > timeout:
                    timed_out = True
                    os.killpg(process.pid, signal.SIGKILL)
                    break
                if observer is None:
                    time.sleep(0.1)
                elif select.select([observer], [], [], 0.1)[0]:
                    event = observer.recv(1)
                    if not event:
                        observer.close()
                        observer = None
                    else:
                        abort = hooks.measurement(process.pid, event)
                        if abort is not None:
                            os.killpg(process.pid, signal.SIGKILL)
                            break
                        observer.sendall(event)
            if hooks:
                _await_exit(process)
                if abort is None:
                    abort = hooks.exited(process.pid)
            process.wait()
            if hooks:
                abort = hooks.reaped(process.pid, process.returncode) or abort
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    if abort is not None:
        raise abort
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
        log_path = directory / "worker.log"
        with log_path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 4096))
            log_tail = stream.read().decode("utf-8", errors="replace").strip()
        reason = "Worker timed out" if timed_out else "Worker exited before completing the batch"
        if log_tail:
            reason += f": {log_tail}"
        results.append(
            {
                "protocol": PROTOCOL,
                "status": "error",
                "mode": job["mode"],
                "seed": pending[0],
                "error": reason,
                "returncode": process.returncode,
                "log": str(log_path),
            }
        )
        if timed_out and len(pending) > 1:
            # The first missing seed was interrupted. Run the later seeds in a
            # fresh process so one slow compile cannot discard the rest.
            retry_job = dict(job, seeds=pending[1:])
            results.extend(
                run_worker(
                    build, retry_job, directory / f"retry-{pending[1]}", hash_seed, hooks
                )
            )
        elif len(pending) > 1:
            # A start-up or process failure may recur for every seed. Preserve
            # completeness without launching a failing process for each one.
            results.extend(
                {
                    "protocol": PROTOCOL,
                    "status": "error",
                    "mode": job["mode"],
                    "seed": seed,
                    "error": f"Not attempted after abnormal worker exit for seed {pending[0]}",
                    "not_attempted": True,
                    "returncode": process.returncode,
                    "log": str(log_path),
                }
                for seed in pending[1:]
            )
    for row in results:
        row.setdefault("job_file", str(directory / "job.json"))
    return results


def run_verifier_batch(python, pairs, directory, idle_timeout=300):
    """Verify ``pairs`` ({job, out}) in one pinned verifier process.

    The idle timeout applies per job: the process is killed once no new result has appeared
    for ``idle_timeout`` seconds. Returns a reason when it stopped before finishing.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "batch.json", pairs)
    command = [python, "-P", "-m", "qtb_verifier", "--batch", str(directory / "batch.json")]
    outs = [Path(pair["out"]) for pair in pairs]
    done, last, timed_out = 0, time.monotonic(), False
    with (directory / "verifier.log").open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=directory,
            env=sanitized_environment(),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            while process.poll() is None:
                count = sum(out.exists() for out in outs[done:]) + done
                if count > done:
                    done, last = count, time.monotonic()
                elif time.monotonic() - last > idle_timeout:
                    timed_out = True
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    break
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    if all(out.exists() for out in outs):
        return None
    if timed_out:
        return f"Verifier timed out after {idle_timeout} s; log: {directory / 'verifier.log'}"
    return f"Verifier exited {process.returncode}; log: {directory / 'verifier.log'}"
