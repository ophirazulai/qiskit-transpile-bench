"""The baseline store: builds, published results and what may never be written to it."""

import sys
import threading
import time
from pathlib import Path

import pytest

from qtb.canonical import digest, file_hash, read_json, write_json
from qtb.coordinator import Comparison
from qtb.coordinator.storage import quality_entry_valid
from qtb.coordinator.store import Store, build_key, resolve_store
from qtb.errors import HarnessError, Usage


def _comparison(tmp_path, store):
    comparison = Comparison.__new__(Comparison)
    comparison.directory = tmp_path / "session"
    comparison.directory.mkdir(exist_ok=True)
    comparison.stage = "compile"
    comparison.store = store
    comparison.data = Path(__file__).resolve().parents[1]
    comparison.run = {"hashes": {"harness": "harness-hash"}}
    comparison.progress = lambda *_: None
    return comparison


def _fake_build_into(calls, delay=0.0):
    def build_into(snapshot_info, destination, identity, *args, **kwargs):
        calls.append(destination)
        time.sleep(delay)
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=False)
        (destination / "env/bin").mkdir(parents=True)
        (destination / "source").mkdir()
        (destination / "wheels").mkdir()
        (destination / "env/native.so").write_bytes(b"native")
        build = {
            "id": digest(identity),
            "python": str(destination / "env/bin/python"),
            "environment": str(destination / "env"),
            "wheel": str(destination / "wheels/qiskit.whl"),
            "snapshot": dict(snapshot_info, path=str(destination / "source")),
            "provenance": {
                "qiskit_file": str(destination / "env/native.so"),
                "native_file": str(destination / "env/native.so"),
                "native_sha256": file_hash(destination / "env/native.so"),
            },
        }
        write_json(destination / "build.json", build)
        return build

    return build_into


def test_resolve_store_needs_an_existing_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("QTB_STORE", raising=False)
    with pytest.raises(Usage, match="no default"):
        resolve_store(None)
    with pytest.raises(Usage, match="does not exist"):
        resolve_store(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()
    monkeypatch.setenv("QTB_STORE", str(tmp_path))
    assert resolve_store(None) == tmp_path.resolve()


def test_racing_sessions_build_one_key_once(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("qtb.coordinator.build_into", _fake_build_into(calls, delay=0.3))
    store = Store(tmp_path / "store")
    identity = {"snapshot": "tree"}
    results = []

    def session(name):
        comparison = _comparison(tmp_path / name, store)
        results.append(comparison.baseline_build({"tree_hash": "tree"}, identity, "w", "1.89"))

    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    threads = [threading.Thread(target=session, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1
    assert sorted(r["reused"] for r in results) == [False, True]
    key = build_key(identity, "harness-hash")
    assert {r["store_key"] for r in results} == {key}
    assert (store.build_dir(key) / "READY").exists()


def test_an_interrupted_build_is_repaired_under_its_lock(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("qtb.coordinator.build_into", _fake_build_into(calls))
    store = Store(tmp_path / "store")
    identity = {"snapshot": "tree"}
    key = build_key(identity, "harness-hash")
    partial = store.build_dir(key)
    (partial / "env").mkdir(parents=True)
    (partial / "leftover").write_text("killed mid-build")
    assert store.ready_build(key) is None
    build = _comparison(tmp_path, store).baseline_build({"tree_hash": "tree"}, identity, "w", "x")
    assert calls == [partial] and not build["reused"]
    assert not (partial / "leftover").exists() and (partial / "READY").exists()


def test_a_stored_build_must_point_only_inside_its_entry(tmp_path):
    store = Store(tmp_path / "store")
    entry = store.build_dir("k")
    entry.mkdir(parents=True)
    (entry / "READY").write_text("x")
    build = {
        "python": str(entry / "env/bin/python"),
        "environment": str(entry / "env"),
        "wheel": str(entry / "wheels/q.whl"),
        "snapshot": {"path": str(tmp_path / "some-session/builds/baseline")},
    }
    write_json(entry / "build.json", build)
    with pytest.raises(HarnessError, match="own source tree"):
        store.ready_build("k")
    write_json(entry / "build.json", dict(build, python=str(tmp_path / "session/env/python")))
    with pytest.raises(HarnessError, match="outside its entry"):
        store.ready_build("k")


def test_a_stored_venv_interpreter_may_link_to_its_base_python(tmp_path):
    store = Store(tmp_path / "store")
    entry = store.build_dir("k")
    (entry / "env/bin").mkdir(parents=True)
    (entry / "source").mkdir()
    (entry / "READY").write_text("x")
    base = tmp_path / "uv/python/bin/python3.12"
    base.parent.mkdir(parents=True)
    base.write_text("")
    (entry / "env/bin/python3.12").symlink_to(base)
    (entry / "env/bin/python").symlink_to("python3.12")
    build = {
        "python": str(entry / "env/bin/python"),
        "environment": str(entry / "env"),
        "wheel": str(entry / "wheels/q.whl"),
        "snapshot": {"path": str(entry / "source")},
    }
    write_json(entry / "build.json", build)
    assert store.ready_build("k") == build


def test_published_results_appear_atomically_and_first_wins(tmp_path):
    store = Store(tmp_path / "store")

    def fill(text):
        def write(partial, final):
            assert not final.exists()
            (partial / "rows.jsonl").write_text(text)

        return write

    assert store.read_results("correctness", "k") is None
    entry = store.publish_results("correctness", "k", fill("first\n"), {"session": "a"})
    store.publish_results("correctness", "k", fill("second\n"), {"session": "b"})
    assert (entry / "rows.jsonl").read_text() == "first\n"
    assert store.first_computed("correctness", "k")
    assert [p.name for p in (tmp_path / "store/correctness").iterdir()] == ["k"]
    with pytest.raises(HarnessError):
        store.results_dir("cost", "k")


def _upstream_comparison(tmp_path, store, same=False):
    entry = tmp_path / "store-build"
    (entry / "source/test/python/transpiler").mkdir(parents=True, exist_ok=True)
    (entry / "source/test/python/compiler").mkdir(exist_ok=True)
    (entry / "source/test/python/transpiler/test_a.py").write_text(
        "def test_ok():\n    assert True\n"
    )
    (entry / "env").mkdir(exist_ok=True)
    baseline = {
        "id": "base",
        "python": sys.executable,
        "environment": str(entry / "env"),
        "toolchain_channel": "stable",
        "snapshot": {
            "path": str(entry / "source"),
            "files": {"test/python/transpiler/test_a.py": {"sha256": "t"}},
        },
    }
    session = tmp_path / "session"
    (session / "builds/evolved-build/env").mkdir(parents=True, exist_ok=True)
    (session / "builds/evolved-build/cargo").mkdir(exist_ok=True)
    evolved = dict(
        baseline,
        id="base" if same else "idea",
        environment=str(session / "builds/evolved-build/env"),
    )
    comparison = Comparison.__new__(Comparison)
    comparison.directory = session
    comparison.stage = "unit-tests"
    comparison.store = store
    comparison.machine = {"cpu": "test-cpu"}
    comparison.prefix = "CA"
    comparison.records = []
    comparison.data = Path(__file__).resolve().parents[1]
    comparison.policy = {"upstream_test_budgets_s": {"python": 60, "rust": 60}}
    comparison.run = {
        "builds": {"baseline": baseline, "evolved": evolved},
        "hashes": {"harness": "h"},
        "changed_paths": [],
    }
    comparison.progress = lambda *_: None
    return comparison, entry


def _tree(root):
    return {
        str(p.relative_to(root)): file_hash(p) for p in sorted(Path(root).rglob("*")) if p.is_file()
    }


def test_unit_tests_leave_a_stored_build_untouched_and_reuse_its_results(tmp_path, monkeypatch):
    from qtb.coordinator import upstream

    homes, targets = [], []

    def cargo(command, cwd, env, log, timeout):
        homes.append(env["CARGO_HOME"])
        targets.append(env["CARGO_TARGET_DIR"])
        Path(env["CARGO_TARGET_DIR"], "debug").mkdir(parents=True)
        log.write_text("test a::b ... ok\ntest result: ok.\n")

    monkeypatch.setattr(upstream, "run_logged", cargo)
    store = Store(tmp_path / "store")
    comparison, entry = _upstream_comparison(tmp_path, store)
    before = _tree(entry)
    upstream.upstream_checks(comparison)
    assert _tree(entry) == before
    assert not any((entry / "source").rglob("__pycache__"))
    session = comparison.directory
    assert homes[0] == str(session / "upstream-baseline/cargo")
    assert homes[1] == str(session / "builds/evolved-build/cargo")
    assert all(not Path(target).exists() for target in targets)
    assert (session / "upstream-baseline/tests.jsonl.gz").exists()
    records = {r["id"]: r for r in comparison.records}
    assert records["CA1/upstream"]["result"] == "passed"

    [stored] = list((tmp_path / "store/unit-tests").iterdir())
    python = read_json(stored / "python.json")
    assert Path(python["records"]).parent == stored and Path(python["log"]).parent == stored
    assert Path(python["records"]).exists()

    # A later session with the same baseline reads the baseline half from the store.
    later, _ = _upstream_comparison(tmp_path / "later", store)
    ran = []
    monkeypatch.setattr(
        upstream, "python_suite",
        lambda build, *a: ran.append(build["id"]) or dict(completed=True, failed=[], passed=[
            "test/python/transpiler/test_a.py::test_ok"
        ], records="r", log="l"),
    )
    upstream.upstream_checks(later)
    assert "base" not in ran
    baseline = next(r for r in later.records if r["id"] == "CA1/upstream/baseline")
    assert baseline["cached_from"] == upstream.baseline_unit_tests_key(later)


def test_incomplete_or_aa_unit_tests_are_not_stored(tmp_path, monkeypatch):
    from qtb.coordinator import upstream

    monkeypatch.setattr(upstream, "run_logged", lambda *a, **k: None)  # cargo never completes
    store = Store(tmp_path / "store")
    comparison, _ = _upstream_comparison(tmp_path, store)
    upstream.upstream_checks(comparison)
    assert not (tmp_path / "store/unit-tests").exists()
    aa, _ = _upstream_comparison(tmp_path / "aa", store, same=True)
    assert upstream.baseline_unit_tests_key(aa) is None


def test_a_failed_audit_invalidates_the_stored_baseline_quality(tmp_path):
    store = Store(tmp_path / "store")
    comparison = Comparison.__new__(Comparison)
    comparison.directory = tmp_path / "session"
    comparison.stage = "quality"
    comparison.store = store
    comparison.machine = {}
    comparison.records = []
    comparison.policy = {"rng_seed": 1, "measurement_protocol": {}}
    comparison.run = {
        "builds": {"baseline": {"id": "base"}, "evolved": {"id": "idea"}},
        "hashes": {"implementation": "i"},
    }
    comparison.progress = lambda *_: None
    case = {"case_id": "c", "options": {}}
    rows = [
        {"case_id": "c", "revision": revision, "seed": 0, "seed_block": "B0",
         "output_hash": "h", "layout": None}
        for revision in ("baseline", "evolved")
    ]
    entry = store.quality_dir(comparison.baseline_quality_key(case))
    entry.mkdir(parents=True)
    comparison.jobs = lambda specs: [[{"status": "ok", "output_hash": "other"}] for _ in specs]
    comparison.audit([case], rows)
    assert comparison.records[-1]["result"] == "unresolved"
    assert not quality_entry_valid(entry)
