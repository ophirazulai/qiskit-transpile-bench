import subprocess
import sys
from pathlib import Path

import pytest
import qiskit._accelerate as native

from qtb.canonical import file_hash
from qtb.config import data_root, load_profile, validate
from qtb.coordinator.process import run_worker
from qtb.coordinator.storage import quality_cache_key, register_decision
from qtb.envbuild import SERIAL, diff_snapshots, sanitized_environment, snapshot
from qtb.errors import HarnessError


def local_build():
    return dict(
        id="a" * 64,
        python=sys.executable,
        environment=sys.prefix,
        provenance={"native_sha256": file_hash(native.__file__)},
    )


def test_core_imports_no_qiskit():
    result = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            "import sys,qtb.cli; "
            "from qtb.config import implementation_identity; implementation_identity(); "
            'assert "qiskit" not in sys.modules',
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_environment_drops_adversarial_settings(monkeypatch):
    monkeypatch.setenv("QISKIT_SABRE_ALL_THREADS", "FALSE")
    monkeypatch.setenv("PYTHONPATH", "/hostile")
    monkeypatch.setenv("RUSTFLAGS", "-C opt-level=0")
    env = sanitized_environment()
    assert not {"QISKIT_SABRE_ALL_THREADS", "PYTHONPATH", "RUSTFLAGS"} & env.keys()
    assert all(env[k] == v for k, v in SERIAL.items())


def test_snapshot_non_git_dirty_source_and_external_symlink(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.py").write_text("hello")
    (source / ".venv").mkdir()
    (source / ".venv" / "secret").write_text("skip")
    a = snapshot(source, tmp_path / "a")
    (source / "a.py").write_text("different")
    b = snapshot(source, tmp_path / "b")
    assert a["tree_hash"] != b["tree_hash"]
    assert diff_snapshots(a, b) == ["a.py"]
    assert ".venv/secret" not in a["files"]
    (source / "escape").symlink_to("/etc/passwd")
    with pytest.raises(HarnessError):
        snapshot(source, tmp_path / "unsafe")


def test_cache_scopes_and_decision_count(tmp_path):
    case = {"case_id": "a", "options": {}, "weight": 1, "semantic_reference": {"kind": "input"}}
    a = quality_cache_key({"id": "x"}, case, {}, {}, "h")
    assert a == quality_cache_key({"id": "x"}, dict(case, weight=0.5), {}, {}, "h")
    assert a == quality_cache_key({"id": "x"}, dict(case, case_id="confirm/a"), {}, {}, "h")
    assert a != quality_cache_key(
        {"id": "x"}, dict(case, options={"optimization_level": 3}), {}, {}, "h"
    )
    with pytest.raises(HarnessError):
        quality_cache_key({"id": "x"}, case, {}, {}, "h", mode="memory")
    assert register_decision(tmp_path, "m", "r1") == 0
    assert register_decision(tmp_path, "m", "r2") == 1
    assert register_decision(tmp_path, "m", "r1") == 0


@pytest.mark.parametrize("git", [False, True])
def test_snapshot_preserves_nested_target_source(tmp_path, git):
    source = tmp_path / "source"
    rust = source / "crates/transpiler/src/target/mod.rs"
    rust.parent.mkdir(parents=True)
    rust.write_text("pub struct Target;\n")
    (source / "target").mkdir()
    (source / "target/generated").write_text("artifact")
    if git:
        subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
        (source / ".gitignore").write_text("/target/\n")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            check=True,
            capture_output=True,
        )
    saved = snapshot(source, tmp_path / "snapshot")
    assert "crates/transpiler/src/target/mod.rs" in saved["files"]
    assert "target/generated" not in saved["files"]


def test_worker_roundtrip_and_compile(tmp_path):
    manifest, _, _ = load_profile("iterations-profile")
    case = next(c for c in manifest["cases"] if c["case_id"] == "T1")
    job = dict(
        mode="roundtrip",
        case=case,
        seeds=[0],
        fixture_root=str(data_root() / "fixtures"),
        timeout_s=30,
    )
    row = run_worker(local_build(), job, tmp_path / "roundtrip")[0]
    assert row["status"] == "ok", row
    assert row["circuit_hash"] == case["circuit"]["sha256"]
    assert row["target_hash"] == case["target"]["sha256"]
    row = run_worker(local_build(), dict(job, mode="quality"), tmp_path / "quality")[0]
    assert row["status"] == "ok", row
    assert "D2" not in row and "N2" not in row
    assert Path(row["output"]).exists()


def test_unknown_protocol_rejected():
    with pytest.raises(HarnessError):
        validate("job", {"protocol": "qtb-worker/9"})


def test_worker_api_and_negative_configuration_contracts(tmp_path):
    from qtb.canonical import read_json

    case = next(
        c
        for c in read_json(data_root() / "fixtures/correctness-suite.json")["cases"]
        if c["input_group"] == "c1_n3"
        and c["optimization_level"] == 2
        and c["options"]["initial_layout"] is None
    )
    job = dict(
        mode="api_checks",
        case=case,
        seeds=[0],
        fixture_root=str(data_root() / "fixtures"),
        timeout_s=30,
    )
    row = run_worker(local_build(), job, tmp_path / "api")[0]
    assert row["status"] == "ok", row
    assert row["first"] == row["again"]
    assert row["first_layout"] == row["again_layout"]
    assert row["batch_layouts"] == row["individual_layouts"]
    assert row["batch_metadata"] == row["input_metadata"]
    assert all(r["exception_type"] == "TranspilerError" for r in row["negative_tests"]), row[
        "negative_tests"
    ]


def test_append_repairs_only_incomplete_tail(tmp_path):
    from qtb.coordinator.storage import append_record, read_records

    path = tmp_path / "observations.jsonl"
    append_record(path, {"seed": 0})
    with path.open("ab") as stream:
        stream.write(b'{"seed":1')
    assert read_records(path) == [{"seed": 0}]
    append_record(path, {"seed": 2})
    assert read_records(path) == [{"seed": 0}, {"seed": 2}]


def test_wheel_cache_preserves_independent_builds_and_invalidates_source(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    import qtb.envbuild as envbuild

    source = tmp_path / "source"
    source.mkdir()
    (source / "Cargo.lock").write_text("frozen")
    locks = tmp_path / "locks"
    locks.mkdir()
    (locks / "common.lock").write_text("dependency==1")
    wheel = tmp_path / "harness.whl"
    wheel.write_bytes(b"harness")
    info = dict(path=str(source), tree_hash="source1")
    compiles = []

    def command(args, cwd, env, log, timeout=3600):
        args = list(map(str, args))
        if args[1:3] == ["-m", "venv"]:
            root = Path(args[3])
            (root / "bin").mkdir(parents=True)
            (root / "bin/python").write_text("python")
            (root / "qiskit.py").write_text("package")
            (root / "native.so").write_bytes(b"native")
        elif "wheel" in args:
            output = Path(args[args.index("-w") + 1])
            (output / "qiskit-test.whl").write_bytes(b"wheel")
            compiles.append(str(cwd))

    def subprocess_run(args, **kwargs):
        args = list(map(str, args))
        if args[0] == "rustc":
            return SimpleNamespace(returncode=0, stdout="rustc 1.89", stderr="")
        if "-c" in args:
            root = Path(args[0]).parent.parent
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=json.dumps(
                    dict(
                        qiskit_file=str(root / "qiskit.py"),
                        native_file=str(root / "native.so"),
                        qiskit_version="test",
                    )
                ),
            )
        return SimpleNamespace(returncode=0, stdout="qiskit==test", stderr="")

    monkeypatch.setattr(envbuild, "run_logged", command)
    monkeypatch.setattr(envbuild.subprocess, "run", subprocess_run)
    cache = tmp_path / "cache"
    first = envbuild.build_revision(info, tmp_path / "a", locks, wheel, "1.89", cache, "baseline")
    second = envbuild.build_revision(info, tmp_path / "b", locks, wheel, "1.89", cache, "baseline")
    control = envbuild.build_revision(info, tmp_path / "c", locks, wheel, "1.89", cache, "control")
    changed = envbuild.build_revision(
        dict(info, tree_hash="source2"), tmp_path / "d", locks, wheel, "1.89", cache, "baseline"
    )
    assert len(compiles) == 3
    assert first["id"] == second["id"] == control["id"]
    assert second["wheel_cache_hit"] and not control["wheel_cache_hit"]
    assert changed["id"] != first["id"]
    assert len({b["environment"] for b in (first, second, control, changed)}) == 4


def test_pruning_keeps_failures_and_external_cached_outputs(tmp_path):
    from qtb.coordinator.storage import prune_outputs

    run = tmp_path / "run"
    run.mkdir()
    paths = [run / "success.gz", run / "failed.gz", tmp_path / "cached.gz"]
    for path in paths:
        path.write_bytes(b"x" * 50)
    rows = [
        dict(
            id=str(i),
            output=str(path),
            output_hash="hash",
            checks=[{"status": "mismatch" if i == 1 else "verified"}],
        )
        for i, path in enumerate(paths)
    ]
    result = prune_outputs(run, rows, 20)
    assert len(result) == 1
    assert not paths[0].exists() and paths[1].exists() and paths[2].exists()


def test_batched_routing_prefixes_preserve_per_seed_checks(tmp_path):
    from qtb.canonical import read_json
    from qtb.coordinator import Comparison

    comparison = Comparison(
        tmp_path, tmp_path, results_root=tmp_path / "results", progress=lambda *_: None
    )
    comparison.run["builds"] = {"baseline": local_build()}
    case = next(
        c
        for c in read_json(data_root() / "fixtures/correctness-suite.json")["cases"]
        if c["input_group"] == "c1_n3"
        and c["optimization_level"] == 2
        and c["target"]["file"].endswith("5_cx.target.json")
        and c["options"]["initial_layout"] is None
    )
    case = dict(case, role="scored")
    prefixes = comparison.routing_batch("baseline", case, [0, 1])
    assert set(prefixes) == {0, 1}
    assert prefixes[0][0]["job_file"] == prefixes[1][0]["job_file"]
    outputs = comparison.job("baseline", case, "quality", [0, 1])
    for result in outputs:
        assert result["status"] == "ok"
        observation = dict(
            seed=result["seed"],
            layout=result["layout"],
            checks=[],
            reference_hash=case["circuit"]["sha256"],
        )
        comparison.check_routing("baseline", case, observation, prefixes)
        assert observation["checks"][0]["status"] == "verified", observation


def test_build_timeout_terminates_descendants(tmp_path):
    from qtb.envbuild import run_logged

    # A descendant would write this marker after its parent times out unless
    # the entire build process group is terminated.
    marker = tmp_path / "orphan-finished"
    child = (
        "import time; from pathlib import Path; time.sleep(1); Path("
        + repr(str(marker))
        + ").touch()"
    )
    parent = (
        'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",'
        + repr(child)
        + "]); time.sleep(10)"
    )
    with pytest.raises(HarnessError, match="timed out"):
        run_logged(
            [sys.executable, "-c", parent],
            tmp_path,
            sanitized_environment(),
            tmp_path / "build.log",
            timeout=0.5,
        )
    import time

    time.sleep(1)
    assert not marker.exists()
