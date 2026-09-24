"""Self-contained single-observation reproducers from archived wheels and fixtures."""

import shutil
from pathlib import Path

from qtb.canonical import read_json, safe_path, write_json
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
'''


def export_reproducer(observation, run_directory, destination, locks):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    job = read_json(observation["worker"]["job_file"])
    job["seeds"] = [observation["seed"]]
    case = job["case"]
    artifacts = [case["circuit"], case["target"]]
    if case["semantic_reference"]["kind"] == "frozen_circuit":
        artifacts.append(case["semantic_reference"])
    for artifact in artifacts:
        source = safe_path(job["fixture_root"], artifact["file"])
        output = safe_path(destination/"fixtures", artifact["file"])
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
    wheels = destination/"wheels"
    wheels.mkdir()
    revision_wheel = Path(job["build"].get("wheel", ""))
    if not revision_wheel.is_file():
        raise HarnessError("Reproducer requires the archived revision wheel")
    shutil.copy2(revision_wheel, wheels/revision_wheel.name)
    harness_wheels = list((Path(run_directory)/"harness-wheel").glob("*.whl"))
    if len(harness_wheels) != 1:
        raise HarnessError("Reproducer requires one archived harness wheel")
    shutil.copy2(harness_wheels[0], wheels/harness_wheels[0].name)
    shutil.copy2(Path(locks)/"common.lock", destination/"common.lock")
    write_json(destination/"job.json", job)
    write_json(destination/"observation.json", observation)
    (destination/"run.py").write_text(RUNNER)
    (destination/"README.md").write_text(
        "# Single-observation reproducer\n\nRun `python run.py` with the recorded Python version.\n"
        "The script installs the archived Qiskit and harness wheels into a fresh environment,\n"
        "loads the frozen artifacts, verifies the native extension, and compiles the one recorded seed.\n")
    return destination
