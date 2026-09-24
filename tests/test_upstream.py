import sys

from qtb.coordinator.upstream import python_suite


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
        {"python": sys.executable}, source, tmp_path / "suite", tmp_path
    )

    assert result["result"] == "failed"
    assert result["passed"] == ["test/python/transpiler/test_example.py::test_first"]
    assert result["failed"] == ["test/python/transpiler/test_example.py::test_second"]
