import sys
from types import SimpleNamespace

from qtb.config import load_profile
from qtb.coordinator.upstream import python_suite, rust_suite


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

    result = python_suite(
        {"python": sys.executable}, source, tmp_path / "suite", tmp_path, 60
    )

    assert result["result"] == "failed"
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
    python_suite({"python": sys.executable}, source, tmp_path / "suite", tmp_path, 14400)

    def logged(*args, **kwargs):
        seen["rust"] = kwargs["timeout"]

    monkeypatch.setattr("qtb.coordinator.upstream.run_logged", logged)
    build = {"toolchain_channel": "stable", "environment": str(tmp_path / "env")}
    assert rust_suite(build, tmp_path / "rust", 10800) == "passed"
    assert seen == {"python": 14400, "rust": 10800}
