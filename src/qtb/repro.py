"""Self-contained single-observation reproducers from archived wheels and fixtures."""

import zipfile
from pathlib import Path

from qtb.canonical import file_hash, read_json, safe_path, verify_artifact, write_json
from qtb.config import PROTOCOL, case_hash
from qtb.errors import HarnessError

RUNNER = '''"""Recreate this observation from the archived, non-editable wheels."""
import json
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
env = root / "env"
if not env.exists():
    subprocess.run([sys.executable, "-m", "venv", str(env)], check=True)
python = env / "bin/python"
subprocess.run([str(python), "-m", "pip", "install", "-r", str(root / "common.lock"),
                *map(str, sorted((root / "wheels").glob("*.whl")))], check=True)
job = json.loads((root / "job.json").read_text())
job["fixture_root"] = str(root / "fixtures")
job["build"]["python"] = str(python)
job["build"]["environment"] = str(env)
(root / "resolved-job.json").write_text(json.dumps(job))
scratch = root / "scratch"
scratch.mkdir(exist_ok=True)
# The installed harness owns environment sanitation and provenance validation.
code = ("import subprocess,sys; from qtb.envbuild import sanitized_environment; "
        "raise SystemExit(subprocess.run(sys.argv[1:],env=sanitized_environment()).returncode)")
subprocess.run([str(python), "-P", "-c", code, str(python), "-P", "-m", "qtb_worker",
                "--job", str(root / "resolved-job.json"), "--out", str(root / "output")],
               cwd=scratch, check=True)
result = json.loads((root / "output/results.jsonl").read_text().splitlines()[0])
expected = json.loads((root / "observation.json").read_text())["worker"]
match = (result["status"] == expected["status"] and
         result.get("output_hash") == expected.get("output_hash"))
(root / "reproduction.json").write_text(json.dumps({"match": match,
    "expected_status": expected["status"], "actual_status": result["status"],
    "expected_output_hash": expected.get("output_hash"),
    "actual_output_hash": result.get("output_hash")}, sort_keys=True) + "\\n")
print("Observation reproduced" if match else "Observation differs; see reproduction.json")
raise SystemExit(0 if match else 1)
'''


def _archived_path(path, run_directory, run):
    """Resolve a path in a run relocated since its original execution."""
    original = Path(path)
    if run and run.get("run_id") and run.get("results_root"):
        old_run = Path(run["results_root"]) / "runs" / run["run_id"]
        if original.is_relative_to(old_run):
            relocated = safe_path(run_directory, original.relative_to(old_run))
            if relocated.exists():
                return relocated
    return original


def _cached_job(observation, run_directory, run):
    """Recreate a cached quality job if its source run is no longer available."""
    if observation.get("mode") != "quality" or not run:
        raise HarnessError("Reproducer requires the archived worker job")
    manifest = read_json(run_directory / "manifest.json")
    cases = [case for case in manifest["cases"] if case["case_id"] == observation["case_id"]]
    if len(cases) != 1 or case_hash(cases[0]) != observation["case_hash"]:
        raise HarnessError("Cached observation does not match this run's manifest")
    build = run["builds"][observation["revision"]]
    if build["id"] != observation["build_id"]:
        raise HarnessError("Cached observation does not match this run's build")
    case = cases[0]
    return {
        "protocol": PROTOCOL,
        "mode": "quality",
        "case": case,
        "seeds": [observation["seed"]],
        "fixture_root": str(run_directory / "fixtures"),
        "timeout_s": case["timeout_s"],
        "pipeline_edits": [],
        "bindings": case.get("bindings", []),
        "build": build,
    }


def export_reproducer(observation, run_directory, destination):
    run_directory = Path(run_directory).resolve()
    destination = Path(destination).resolve()
    run = read_json(run_directory / "run.json") if (run_directory / "run.json").exists() else None
    job_path = _archived_path(observation["worker"]["job_file"], run_directory, run)
    job = (
        read_json(job_path)
        if job_path.is_file()
        else _cached_job(observation, run_directory, run)
    )
    job["seeds"] = [observation["seed"]]
    harness_wheels = list((run_directory / "harness-wheel").glob("*.whl"))
    if len(harness_wheels) != 1:
        raise HarnessError("Reproducer requires one archived harness wheel")
    revision_wheel = _archived_path(job["build"].get("wheel", ""), run_directory, run)
    if not revision_wheel.is_file():
        raise HarnessError("Reproducer requires the archived revision wheel")
    expected_wheel_hash = job["build"].get("wheel_sha256")
    if expected_wheel_hash and file_hash(revision_wheel) != expected_wheel_hash:
        raise HarnessError("Archived revision wheel hash differs from job provenance")
    destination.mkdir(parents=True, exist_ok=False)
    case = job["case"]
    artifacts = [case["circuit"], case["target"]]
    if case["semantic_reference"]["kind"] == "frozen_circuit":
        artifacts.append(case["semantic_reference"])
    with zipfile.ZipFile(harness_wheels[0]) as archive:
        names = set(archive.namelist())
        for artifact in artifacts:
            output = safe_path(destination / "fixtures", artifact["file"])
            output.parent.mkdir(parents=True, exist_ok=True)
            member = "qtb/data/fixtures/" + artifact["file"]
            if member in names:
                output.write_bytes(archive.read(member))
            else:
                source = safe_path(job["fixture_root"], artifact["file"])
                if not source.is_file():
                    raise HarnessError(f"Reproducer requires archived fixture {artifact['file']}")
                output.write_bytes(source.read_bytes())
            if "sha256" in artifact:
                verify_artifact(destination / "fixtures", artifact, artifact is case["circuit"])
        (destination / "common.lock").write_bytes(archive.read("qtb/data/envs/common.lock"))
    wheels = destination / "wheels"
    wheels.mkdir()
    (wheels / revision_wheel.name).write_bytes(revision_wheel.read_bytes())
    (wheels / harness_wheels[0].name).write_bytes(harness_wheels[0].read_bytes())
    job["build"]["wheel"] = str(wheels / revision_wheel.name)
    write_json(destination / "job.json", job)
    write_json(destination / "observation.json", observation)
    (destination / "run.py").write_text(RUNNER)
    (destination / "README.md").write_text(
        "# Single-observation reproducer\n\nRun `python run.py` with the recorded Python version.\n"
        "The script installs the archived Qiskit and harness wheels into a fresh environment,\n"
        "loads the frozen artifacts, verifies the native extension, "
        "and compiles the one recorded seed. It writes `reproduction.json` with the\n"
        "recorded and reproduced status and output hash.\n"
    )
    return destination
