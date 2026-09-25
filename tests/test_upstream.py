import sys
from types import SimpleNamespace

from qtb.config import load_profile
from qtb.coordinator.upstream import judge, pytest_config, python_suite, rust_suite


def test_upstream_suite_runs_baseline_tests_without_exclusions(tmp_path, monkeypatch):
    source = tmp_path / "source"
    transpiler = source / "test/python/transpiler"
    transpiler.mkdir(parents=True)
    (source / "test/python/compiler").mkdir()
    (transpiler / "test_example.py").write_text(
        "def test_first():\n    assert True\n"
        "def test_second():\n    assert False\n"
    )
    monkeypatch.setattr("qtb.coordinator.upstream.run_logged", lambda *args, **kwargs: None)

    result = python_suite({"python": sys.executable}, source, tmp_path / "suite", 60)

    assert result["completed"]
    assert result["passed"] == ["test/python/transpiler/test_example.py::test_first"]
    assert result["failed"] == ["test/python/transpiler/test_example.py::test_second"]


def test_upstream_time_budgets_are_frozen_and_used(tmp_path, monkeypatch):
    for profile in ("iterations-profile", "confirm-profile"):
        _, policy, _ = load_profile(profile, verify=False)
        assert policy["upstream_test_budgets_s"] == {"python": 14400, "rust": 10800}

    source = tmp_path / "source"
    (source / "test").mkdir(parents=True)
    seen = {}
    monkeypatch.setattr("qtb.coordinator.upstream.run_logged", lambda *args, **kwargs: None)

    def run(*args, **kwargs):
        seen["python"] = kwargs["timeout"]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("qtb.coordinator.upstream.subprocess.run", run)
    python_suite({"python": sys.executable}, source, tmp_path / "suite", 14400)

    def logged(command, cwd, env, log, timeout):
        seen["rust"] = timeout
        log.write_text("test a::b ... ok\ntest a::c ... FAILED\ntest result: FAILED.\n")

    monkeypatch.setattr("qtb.coordinator.upstream.run_logged", logged)
    build = {"toolchain_channel": "stable", "environment": str(tmp_path / "env")}
    rust = rust_suite(build, tmp_path / "rust", 10800)
    assert rust["completed"] and rust["passed"] == ["a::b"] and rust["failed"] == ["a::c"]
    assert seen == {"python": 14400, "rust": 10800}


def test_upstream_runner_survives_spawned_worker_processes(tmp_path, monkeypatch):
    """Spawned workers re-import the runner; they must not re-run the suite."""
    source = tmp_path / "source"
    compiler = source / "test/python/compiler"
    compiler.mkdir(parents=True)
    (source / "test/python/transpiler").mkdir()
    (compiler / "test_pool.py").write_text(
        "import multiprocessing\n"
        "from concurrent.futures import ProcessPoolExecutor\n"
        "def test_spawn_pool():\n"
        "    context = multiprocessing.get_context('spawn')\n"
        "    with ProcessPoolExecutor(2, mp_context=context) as pool:\n"
        "        assert list(pool.map(abs, [-1, -2])) == [1, 2]\n"
    )
    monkeypatch.setattr("qtb.coordinator.upstream.run_logged", lambda *args, **kwargs: None)

    result = python_suite({"python": sys.executable}, source, tmp_path / "suite", 120)

    assert result["completed"]
    assert result["failed"] == []
    assert result["passed"] == ["test/python/compiler/test_pool.py::test_spawn_pool"]


def test_upstream_uses_only_the_snapshot_pytest_config(tmp_path):
    assert pytest_config(tmp_path) == "[pytest]\n"
    (tmp_path / "tox.ini").write_text("[pytest]\naddopts = -ra\n[testenv]\nx = 1\n")
    assert pytest_config(tmp_path) == "[pytest]\naddopts = -ra\n"


def _suite(failed=(), passed=(), completed=True):
    return {"completed": completed, "failed": list(failed), "passed": list(passed)}


def test_baseline_failures_are_known_bad_not_violations():
    base, evolved, regressions = judge(
        _suite(failed=["t::a"], passed=["t::b"]), _suite(failed=["t::a"], passed=["t::b"])
    )
    assert base[0] == "unresolved" and "known-bad" in base[1]
    assert evolved == ("passed", "")
    assert regressions == []


def test_only_new_failures_fail_the_evolved_revision():
    _, evolved, regressions = judge(
        _suite(failed=["t::a"], passed=["t::b", "t::c"]),
        _suite(failed=["t::a", "t::b"], passed=[]),
    )
    # t::c vanished (for example after a collection error): also a regression.
    assert evolved[0] == "failed"
    assert regressions == ["t::b", "t::c"]


def test_incomplete_suites_are_unresolved():
    _, evolved, _ = judge(_suite(passed=["t::a"]), _suite(passed=["t::a"], completed=False))
    assert evolved[0] == "unresolved"
    base, evolved, _ = judge(_suite(completed=False), _suite(failed=["t::a"]))
    assert base[0] == "unresolved" and evolved[0] == "unresolved"
    _, evolved, _ = judge(_suite(completed=False), _suite(passed=["t::a"]))
    assert evolved[0] == "passed"
