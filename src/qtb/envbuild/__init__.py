"""Immutable snapshots and independent non-editable revision builds."""

import os
import platform
import shutil
import signal
import subprocess
import sys
import tomllib
from pathlib import Path

from qtb.canonical import digest, file_hash, read_json, write_json
from qtb.errors import HarnessError

EXCLUDED = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "build",
    "dist",
    "target",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".idea",
}
SERIAL = {
    "QISKIT_PARALLEL": "FALSE",
    "QISKIT_IGNORE_USER_SETTINGS": "TRUE",
    "RAYON_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONNOUSERSITE": "1",
    "PYTHONSAFEPATH": "1",
}


def sanitized_environment(extra=None):
    keep = {
        "PATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "LANG",
        "LC_ALL",
    }
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(SERIAL)
    env.update(extra or {})
    return env


def machine_identity():
    cpu = platform.processor()
    frequency = {}
    if sys.platform == "darwin":
        proc = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            check=False,
        )
        cpu = proc.stdout.strip() or cpu
    elif Path("/proc/cpuinfo").exists():
        text = Path("/proc/cpuinfo").read_text()
        cpu = next(
            (
                line.split(":", 1)[1].strip()
                for line in text.splitlines()
                if line.startswith("model name")
            ),
            cpu,
        )
        for path in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor"):
            frequency[str(path)] = path.read_text().strip()
    return {
        "host": platform.node(),
        "cpu": cpu,
        "cores": os.cpu_count(),
        "os": platform.system(),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "frequency": frequency,
        "python": platform.python_version(),
    }


def snapshot(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_dir() or destination.is_relative_to(source):
        raise HarnessError("Source must exist and snapshot must be outside source")
    proc = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    git_root = Path(proc.stdout.strip()) if proc.returncode == 0 else None
    if git_root is not None and git_root.resolve() == source:
        listing = subprocess.run(
            ["git", "-C", str(source), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True,
            check=True,
        ).stdout
        names = sorted(set(os.fsdecode(p) for p in listing.split(b"\0") if p))
        commit = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    else:
        names = []
        for root, dirs, files in os.walk(source):
            # Build directories are excluded only at the project root: Qiskit
            # also has real source packages named crates/transpiler/src/target.
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in {".git", "__pycache__"}
                and not (Path(root) == source and (d in EXCLUDED or d.endswith(".egg-info")))
            )
            names.extend(str((Path(root) / f).relative_to(source)) for f in sorted(files))
        commit, dirty = None, None
    destination.mkdir(parents=True, exist_ok=False)
    entries = {}
    for name in names:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise HarnessError("Unsafe snapshot path")
        # Git already filters ignored, untracked build products. Never discard
        # tracked source because a path component happens to be called target.
        if ".git" in relative.parts:
            continue
        original = source / name
        if not original.exists() and not original.is_symlink():
            continue  # deleted tracked file
        if original.is_dir():
            raise HarnessError(f"Submodules must be materialized explicitly: {name}")
        output = destination / name
        output.parent.mkdir(parents=True, exist_ok=True)
        if original.is_symlink():
            target = os.readlink(original)
            if Path(target).is_absolute() or not original.resolve().is_relative_to(source):
                raise HarnessError(f"Snapshot symlink escapes source: {name}")
            output.symlink_to(target)
            entries[name] = {"symlink": target}
        else:
            shutil.copy2(original, output)
            entries[name] = {
                "sha256": file_hash(output),
                "executable": bool(output.stat().st_mode & 0o111),
            }
    if not entries:
        raise HarnessError("Empty source snapshot")
    result = {
        "source": str(source),
        "path": str(destination),
        "commit": commit,
        "dirty": bool(dirty) if dirty is not None else None,
        "dirty_status": dirty,
        "files": entries,
        "tree_hash": digest(entries),
    }
    write_json(destination.parent / f"{destination.name}.snapshot.json", result)
    return result


def diff_snapshots(baseline, evolved):
    a, b = baseline["files"], evolved["files"]
    return sorted(path for path in a.keys() | b.keys() if a.get(path) != b.get(path))


def run_logged(command, cwd, env, log, timeout=3600):
    with Path(log).open("ab") as stream:
        stream.write(("COMMAND " + repr(list(map(str, command))) + "\n").encode())
        stream.flush()
        try:
            proc = subprocess.Popen(
                list(map(str, command)),
                cwd=cwd,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                proc.wait(timeout=timeout)
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HarnessError(f"Build command failed: {exc}; log: {log}") from exc
    if proc.returncode:
        raise HarnessError(f"Build command exited {proc.returncode}; log: {log}")


def baseline_toolchain(snapshot_info):
    path = Path(snapshot_info["path"]) / "rust-toolchain.toml"
    if not path.exists() or shutil.which("rustup") is None:
        raise HarnessError("Qiskit source needs rust-toolchain.toml and rustup")
    return tomllib.loads(path.read_text())["toolchain"]["channel"]


def build_revision(
    snapshot_info,
    destination,
    locks,
    harness_wheel,
    toolchain,
    cache_root=None,
    cache_slot="baseline",
):
    destination, locks = Path(destination).resolve(), Path(locks).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    log = destination / "build.log"
    envdir, source = destination / "env", destination / "source"
    shutil.copytree(snapshot_info["path"], source, symlinks=True)
    cargo = destination / "cargo"
    cargo.mkdir()
    (cargo / "config.toml").write_text("[net]\nretry = 2\n")
    env = sanitized_environment(
        {
            "QISKIT_BUILD_PROFILE": "release",
            "QISKIT_BUILD_WITH_MIMALLOC": "1",
            "RUSTUP_TOOLCHAIN": toolchain,
            "CARGO_HOME": str(cargo),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    tool = subprocess.run(["rustc", "-Vv"], env=env, capture_output=True, text=True, check=False)
    if tool.returncode:
        raise HarnessError(f"Cannot resolve baseline toolchain {toolchain}: {tool.stderr}")
    compilers = {}
    for compiler in ("cc", "c++"):
        version = subprocess.run(
            [compiler, "--version"], env=env, capture_output=True, text=True, check=False
        )
        if version.returncode:
            raise HarnessError(f"Cannot resolve native compiler {compiler}")
        compilers[compiler] = version.stdout
    identity = {
        "snapshot": snapshot_info["tree_hash"],
        "python": sys.version,
        "locks": {p.name: file_hash(p) for p in locks.iterdir() if p.is_file()},
        "toolchain": tool.stdout,
        "native_compilers": compilers,
        "flags": {k: env[k] for k in ("QISKIT_BUILD_PROFILE", "QISKIT_BUILD_WITH_MIMALLOC")},
        "os": platform.system(),
        "architecture": platform.machine(),
    }
    run_logged([sys.executable, "-m", "venv", envdir], destination, env, log)
    python = envdir / "bin/python"
    run_logged(
        [
            python,
            "-m",
            "pip",
            "install",
            "-r",
            locks / "common.lock",
            "-r",
            locks / "build-constraints.txt",
            "-r",
            locks / "dev-tests.lock",
        ],
        destination,
        env,
        log,
    )
    cargo_lock = source / "Cargo.lock"
    before = file_hash(cargo_lock)
    wheel_dir = destination / "wheels"
    wheel_dir.mkdir()
    cache = Path(cache_root) / cache_slot / digest(identity) if cache_root else None
    cache_hit = False
    if cache and (cache / "wheel.json").exists():
        cached = read_json(cache / "wheel.json")
        wheel = cache / cached["name"]
        if cached["identity"] == identity and file_hash(wheel) == cached["sha256"]:
            shutil.copy2(wheel, wheel_dir / wheel.name)
            cache_hit = True
    if not cache_hit:
        run_logged(
            [
                python,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "-w",
                wheel_dir,
                source,
            ],
            destination,
            env,
            log,
        )
    if file_hash(cargo_lock) != before:
        raise HarnessError("Qiskit build modified Cargo.lock")
    wheels = list(wheel_dir.glob("qiskit-*.whl"))
    if len(wheels) != 1:
        raise HarnessError("Build did not produce exactly one Qiskit wheel")
    if cache and not cache_hit:
        from qtb.canonical import atomic_bytes
        from qtb.coordinator.storage import locked

        with locked(cache / "cache.lock"):
            atomic_bytes(cache / wheels[0].name, wheels[0].read_bytes())
            write_json(
                cache / "wheel.json",
                {"identity": identity, "name": wheels[0].name, "sha256": file_hash(wheels[0])},
            )
    run_logged(
        [python, "-m", "pip", "install", "--no-deps", wheels[0], harness_wheel],
        destination,
        env,
        log,
    )
    run_logged([python, "-m", "pip", "check"], destination, env, log)
    code = (
        "import json,qiskit,qiskit._accelerate as native; "
        "print(json.dumps({'qiskit_file':qiskit.__file__,'native_file':native.__file__,"
        "'qiskit_version':qiskit.__version__}))"
    )
    proc = subprocess.run(
        [python, "-P", "-c", code],
        cwd=destination,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode:
        raise HarnessError(f"Built Qiskit cannot import: {proc.stderr}")
    import json

    provenance = json.loads(proc.stdout)
    for key in ("qiskit_file", "native_file"):
        if not Path(provenance[key]).resolve().is_relative_to(envdir):
            raise HarnessError("Import provenance escaped isolated environment")
    provenance["native_sha256"] = file_hash(provenance["native_file"])
    freeze = subprocess.run(
        [python, "-m", "pip", "freeze", "--all"],
        cwd=destination,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result = {
        "id": digest(identity),
        "wheel_cache_hit": cache_hit,
        "toolchain_channel": toolchain,
        "identity": identity,
        "python": str(python),
        "environment": str(envdir),
        "snapshot": snapshot_info,
        "provenance": provenance,
        "wheel": str(wheels[0]),
        "wheel_sha256": file_hash(wheels[0]),
        "pip_freeze": freeze,
    }
    write_json(destination / "build.json", result)
    return result


def verify_build(build):
    provenance = build["provenance"]
    root = Path(build["environment"]).resolve()
    for key in ("qiskit_file", "native_file"):
        if not Path(provenance[key]).resolve().is_relative_to(root):
            raise HarnessError("Build provenance escaped environment")
    if file_hash(provenance["native_file"]) != provenance["native_sha256"]:
        raise HarnessError("Native extension changed after build")
    return read_json(Path(build["environment"]).parent / "build.json")
