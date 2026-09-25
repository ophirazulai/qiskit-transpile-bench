import subprocess
import sys
from pathlib import Path

import pytest
import qiskit._accelerate as native

from qtb.canonical import file_hash
from qtb.config import data_root, load_profile, validate
from qtb.coordinator.process import run_worker
from qtb.coordinator.storage import (
    cache_quality_observation,
    cached_quality_observation,
    quality_cache_key,
)
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


def test_cache_scopes():
    case = {"case_id": "a", "options": {}, "weight": 1, "semantic_reference": {"kind": "input"}}
    a = quality_cache_key({"id": "x"}, case, {}, {}, "h")
    assert a == quality_cache_key({"id": "x"}, dict(case, weight=0.5), {}, {}, "h")
    assert a == quality_cache_key({"id": "x"}, dict(case, case_id="confirm/a"), {}, {}, "h")
    assert a != quality_cache_key(
        {"id": "x"}, dict(case, options={"optimization_level": 3}), {}, {}, "h"
    )
    machine = {"cpu": "M1", "host": "old", "frequency": {"governor": "performance"}}
    stable = quality_cache_key({"id": "x"}, case, machine, {"rounds": 30}, "h")
    assert stable == quality_cache_key(
        {"id": "x"}, dict(case, timeout_s=600, input_group="renamed"),
        dict(machine, host="new", frequency={"governor": "powersave"}),
        {"rounds": 30}, "h",
    )
    assert stable != quality_cache_key(
        {"id": "x"}, case, dict(machine, cpu="M2"), {"rounds": 30}, "h"
    )
    assert stable != quality_cache_key({"id": "x"}, case, machine, {"rounds": 31}, "h")
    # Cost-only protocol settings never reach a quality compile.
    cost_only = {"timing_rounds": 6, "screen_rounds": 4, "minimum_calls": 2, "warmups": 1}
    assert stable == quality_cache_key(
        {"id": "x"}, case, machine, dict({"rounds": 30}, **cost_only), "h"
    )
    with pytest.raises(HarnessError):
        quality_cache_key({"id": "x"}, case, {}, {}, "h", mode="memory")


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


def test_worker_times_a_whole_panel_in_one_process(tmp_path):
    from qtb.coordinator.costs import timing_batch_job

    manifest, policy, _ = load_profile("iterations-profile")
    cases = [c for c in manifest["cases"] if c["case_id"] in {"T1", "T2", "preset/cz"}]
    job = timing_batch_job(cases, data_root() / "fixtures", policy["measurement_protocol"])
    job["minimum_ns"] = 0
    rows = run_worker(local_build(), job, tmp_path / "batch")
    assert [row["status"] for row in rows] == ["ok"] * 3, rows
    assert [row["case_id"] for row in rows] == [c["case_id"] for c in cases]
    assert [row["timing_mode"] for row in rows] == ["timing_e2e", "timing_e2e", "preset_build"]
    assert [row["timing_seed"] for row in rows] == [20220125, 20220125, 0]
    assert all(len(row["samples_ns"]) >= 2 and min(row["samples_ns"]) > 0 for row in rows)
    assert len(list((tmp_path / "batch").glob("retry-*"))) == 0


@pytest.mark.parametrize("failure", ["timeout", "exit"])
def test_worker_batch_records_every_seed_after_interruption(tmp_path, failure):
    manifest, _, _ = load_profile("iterations-profile")
    case = next(c for c in manifest["cases"] if c["case_id"] == "T1")
    fake_python = tmp_path / "fake-worker"
    failure_action = (
        "time.sleep(8)"
        if failure == "timeout"
        else "print('fatal startup', file=sys.stderr); sys.exit(23)"
    )
    fake_python.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys, time\n"
        "args = sys.argv\n"
        "job = json.loads(pathlib.Path(args[args.index('--job') + 1]).read_text())\n"
        "out = pathlib.Path(args[args.index('--out') + 1]) / 'results.jsonl'\n"
        "for seed in job['seeds']:\n"
        f"    if seed == 1: {failure_action}\n"
        "    with out.open('a') as stream:\n"
        "        stream.write(json.dumps({'protocol': job['protocol'], 'status': 'ok', "
        "'mode': job['mode'], 'seed': seed}) + '\\n')\n"
    )
    fake_python.chmod(0o755)
    job = dict(
        mode="quality",
        case=case,
        seeds=[0, 1, 2, 3],
        fixture_root=str(data_root() / "fixtures"),
        timeout_s=2,
    )
    rows = run_worker(dict(local_build(), python=str(fake_python)), job, tmp_path / "batch")
    assert [row["seed"] for row in rows] == [0, 1, 2, 3]
    expected_tail = ["ok", "ok"] if failure == "timeout" else ["error", "error"]
    assert [row["status"] for row in rows] == ["ok", "error", *expected_tail]
    if failure == "timeout":
        assert Path(rows[2]["job_file"]).parent.name == "retry-2"
        assert not rows[2].get("not_attempted")
    else:
        assert all(row["not_attempted"] for row in rows[2:])
        assert "fatal startup" in rows[1]["error"]


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
    wheel_budgets = []
    cargo_homes = []
    build_flags = []

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
            release = Path(args[-1]) / "target/release"
            release.mkdir(parents=True)
            (release / "libqiskit.so").write_bytes(b"release artifacts")
            compiles.append(str(cwd))
            wheel_budgets.append(timeout)
            cargo_homes.append(Path(env["CARGO_HOME"]))
            build_flags.append(
                (
                    env["QISKIT_BUILD_PROFILE"],
                    env["QISKIT_BUILD_WITH_MIMALLOC"],
                    env["CARGO_PROFILE_RELEASE_LTO"],
                    env["CARGO_PROFILE_RELEASE_CODEGEN_UNITS"],
                    "CARGO_NET_OFFLINE" in env,
                )
            )

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
    cache = tmp_path / "store-wheels"

    def build(snapshot_info, name, wheel_cache):
        identity = envbuild.build_identity(snapshot_info, locks, "1.89")
        return envbuild.build_into(
            snapshot_info, tmp_path / name, identity, locks, wheel, "1.89", wheel_cache
        )

    first = build(info, "a", cache)
    second = build(info, "b", cache)
    evolved = build(info, "c", None)
    changed = build(dict(info, tree_hash="source2"), "d", cache)
    assert len(compiles) == 3
    assert first["id"] == second["id"] == evolved["id"]
    assert second["wheel_cache_hit"] and not evolved["wheel_cache_hit"]
    assert changed["id"] != first["id"]
    assert len({b["environment"] for b in (first, second, evolved, changed)}) == 4
    assert wheel_budgets == [envbuild.RUST_WHEEL_TIMEOUT_S] * 3
    assert build_flags == [("release", "1", "thin", "16", False)] * 3
    assert first["identity"]["flags"]["CARGO_PROFILE_RELEASE_LTO"] == "thin"
    # Each build keeps its own CARGO_HOME and crates; there is no shared registry.
    assert len(set(cargo_homes)) == 3
    assert all((path / "config.toml").exists() for path in cargo_homes)
    assert not any((path / "registry").is_symlink() for path in cargo_homes)
    # The build records its own copy of the source and drops the release artifacts.
    for result, name in ((first, "a"), (evolved, "c")):
        assert result["snapshot"]["path"] == str((tmp_path / name / "source").resolve())
        assert result["snapshot"]["tree_hash"] == "source1"
        assert not (tmp_path / name / "source/target/release").exists()


def test_pruning_keeps_failures_and_external_cached_outputs(tmp_path):
    from qtb.coordinator.storage import output_deletions

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
    result = output_deletions(run, rows, 20)
    assert list(result) == [str(paths[0].resolve())]
    assert result[str(paths[0].resolve())]["compressed_sha256"]
    # Planning deletes nothing; clean does, after recording the plan.
    assert all(path.exists() for path in paths)


def test_verified_large_c6_prefixes_prune_with_archived_hashes_and_jobs(tmp_path):
    from qtb.canonical import write_json
    from qtb.coordinator.storage import prefix_output_deletions

    run = tmp_path / "run"
    prefixes = []
    for stage in ("initial", "routed"):
        job_dir = run / "jobs" / stage
        output = job_dir / "out" / "output-0.ops.jsonl.gz"
        output.parent.mkdir(parents=True)
        output.write_bytes(stage.encode() * 20)
        job = job_dir / "job.json"
        write_json(job, {"mode": "prefix", "seeds": [0]})
        prefixes.append(
            {"stage": stage, "output": str(output), "output_hash": stage, "job_file": str(job)}
        )
    unverified = run / "jobs" / "unverified" / "out" / "output-0.ops.jsonl.gz"
    unverified.parent.mkdir(parents=True)
    unverified.write_bytes(b"x" * 80)
    rows = [
        {
            "id": "observation",
            "seed": 0,
            "checks": [{"oracle": "C6", "status": "verified", "prefix_outputs": prefixes}],
        },
        {
            "id": "other",
            "seed": 0,
            "checks": [
                {
                    "oracle": "C6",
                    "status": "unverified",
                    "prefix_outputs": [dict(prefixes[0], output=str(unverified))],
                }
            ],
        },
    ]
    planned = prefix_output_deletions(run, rows, 20)
    assert set(planned) == {str(Path(prefix["output"]).resolve()) for prefix in prefixes}
    assert str(unverified.resolve()) not in planned
    assert {row["output_hash"] for row in planned.values()} == {"initial", "routed"}
    assert all(row["compressed_sha256"] for row in planned.values())
    assert all(Path(row["job_file"]).exists() for row in planned.values())


def test_quality_cache_owns_output_after_session_cleanup(tmp_path):

    run = tmp_path / "run"
    run.mkdir()
    output = run / "compiled.jsonl.gz"
    output.write_bytes(b"compiled" * 20)
    job_file = run / "job.json"
    job_file.write_text('{"seeds":[0]}')
    observation = {
        "id": "id", "output": str(output), "output_hash": "hash",
        "checks": [{"status": "verified"}],
        "worker": {"output": str(output), "job_file": str(job_file)},
    }
    cache = tmp_path / "quality-cache"
    cache_quality_observation(cache, 0, observation)
    cached = cached_quality_observation(cache / "0.json")
    assert cached["output"] != str(output)
    assert cached["worker"]["output"] == cached["output"]
    assert Path(cached["worker"]["job_file"]).read_text() == job_file.read_text()
    output.unlink()  # as clean deletes a session's verified outputs
    assert cached_quality_observation(cache / "0.json") == cached
    Path(cached["output"]).write_bytes(b"tampered")
    assert cached_quality_observation(cache / "0.json") is None


def test_quality_cache_keeps_c6_job_provenance_without_large_prefix_files(tmp_path):
    from qtb.canonical import write_json

    run = tmp_path / "run"
    run.mkdir()
    output = run / "output.gz"
    output.write_bytes(b"quality")
    job = run / "job.json"
    write_json(job, {"mode": "quality", "seeds": [0]})
    prefix_job = run / "prefix" / "job.json"
    prefix_job.parent.mkdir()
    write_json(prefix_job, {"mode": "prefix", "seeds": [0]})
    prefix_output = run / "prefix" / "out" / "output-0.gz"
    prefix_output.parent.mkdir()
    prefix_output.write_bytes(b"large-prefix" * 20)
    observation = {
        "id": "row",
        "output": str(output),
        "worker": {"output": str(output), "job_file": str(job)},
        "checks": [
            {
                "oracle": "C6",
                "status": "verified",
                "prefix_jobs": [str(prefix_job)],
                "prefix_outputs": [
                    {
                        "stage": "initial",
                        "output": str(prefix_output),
                        "output_hash": "canonical-hash",
                        "job_file": str(prefix_job),
                    }
                ],
            }
        ],
    }
    cache = tmp_path / "cache"
    cache_quality_observation(cache, 0, observation)
    prefix_output.unlink()
    cached = cached_quality_observation(cache / "0.json")
    prefix = cached["checks"][0]["prefix_outputs"][0]
    assert prefix["output"] is None
    assert prefix["output_hash"] == "canonical-hash"
    assert Path(prefix["job_file"]).exists()
    assert cached["checks"][0]["prefix_jobs"] == [prefix["job_file"]]
    assert observation["checks"][0]["prefix_outputs"][0]["output"] == str(prefix_output)


def test_unattempted_quality_seed_is_unresolved(tmp_path):
    from qtb.coordinator import Comparison

    manifest, policy, _ = load_profile("iterations-profile")
    case = dict(next(c for c in manifest["cases"] if c["case_id"] == "T1"))
    case["seeds_per_block"] = 2
    comparison = Comparison.__new__(Comparison)
    comparison.directory = tmp_path / "run"
    comparison.directory.mkdir()
    comparison.stage = "quality"
    comparison.store = None
    comparison.machine = {}
    comparison.fixtures = data_root() / "fixtures"
    comparison.run = {
        "builds": {"evolved": {"id": "build"}},
        "machine": {},
        "hashes": {"implementation": "h"},
    }
    comparison.policy = policy
    comparison.records = []
    comparison.progress = lambda *_: None
    comparison.job = lambda *_: [
        {"seed": 0, "status": "error", "error": "Worker timed out"},
        {"seed": 1, "status": "error", "not_attempted": True, "error": "Not attempted"},
    ]
    comparison.quality([case], revisions=("evolved",))
    assert [record["result"] for record in comparison.records] == ["failed", "unresolved"]


def test_batched_routing_prefixes_preserve_per_seed_checks(tmp_path):
    from qtb.canonical import read_json
    from qtb.coordinator import Comparison

    comparison = Comparison.__new__(Comparison)
    comparison.directory = tmp_path / "session"
    comparison.fixtures = data_root() / "fixtures"
    comparison.progress = lambda *_: None
    comparison.run = {"builds": {"baseline": local_build()}}
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


def test_routing_replay_scope_limits_case_guards():
    from qtb.coordinator import Comparison, routing_replay_seeds

    seeds = list(range(25))
    scored = {"role": "scored"}
    basis_guard = {"role": "guard", "panel": "basis-cx"}
    case_guard = {"role": "guard"}
    canary = {"role": "canary"}
    assert routing_replay_seeds(scored, seeds) == seeds
    assert routing_replay_seeds(basis_guard, seeds) == seeds
    assert routing_replay_seeds(case_guard, seeds) == list(range(10))
    assert routing_replay_seeds(canary, seeds) == []

    comparison = Comparison.__new__(Comparison)
    comparison.job = lambda *_: pytest.fail("Canaries must not launch C6 prefix jobs")
    assert comparison.routing_batch("baseline", canary, [0, 1]) == {}
    observation = {"seed": 0, "seed_block": "B0", "checks": []}
    comparison.check_routing("baseline", canary, observation, {})
    assert observation["checks"] == []


@pytest.mark.parametrize("level, expected", [(3, "skipped"), (2, "mismatch")])
def test_routing_records_full_layout_disagreement(monkeypatch, level, expected):
    import qtb.coordinator as coordinator

    monkeypatch.setattr(coordinator, "read_circuit", lambda *_: ({"num_qubits": 2}, []))
    monkeypatch.setattr(coordinator, "replay", lambda *_: {"status": "verified"})
    full = {"initial_index_layout": [0, 1], "final_index_layout": [1, 0]}
    prefix = {"initial_index_layout": [0, 1], "final_index_layout": [0, 1]}
    rows = (
        {"status": "ok", "output": "initial", "layout": prefix, "job_file": "initial-job"},
        {"status": "ok", "output": "routed", "layout": prefix, "job_file": "routed-job"},
    )
    case = {"role": "scored", "optimization_level": level, "input_domain": "all_zero"}
    observation = {
        "seed": 0, "seed_block": "B0", "layout": full,
        "reference_hash": "hash", "checks": [],
    }
    coordinator.Comparison.__new__(coordinator.Comparison).check_routing(
        "evolved", case, observation, {0: rows}
    )
    check = observation["checks"][0]
    assert check["layout_equality"] == expected
    if level == 3:
        assert check["status"] == "verified"
        assert check["layout_disagreement"]["full"] == full
        assert check["layout_disagreement"]["routing_prefix"] == prefix
    else:
        assert check["status"] == "mismatch"


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
