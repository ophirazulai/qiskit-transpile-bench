"""The measurement-facing LSF code is part of the pinned, archived harness."""

import shutil
import tomllib
from pathlib import Path
from types import SimpleNamespace

from qtb import config
from qtb.config import HARNESS_PACKAGES, implementation_identity
from qtb.coordinator import wheel

import lsf
from lsf import MEASUREMENT_MODULES, measurement_identity

ROOT = Path(__file__).resolve().parents[2]


def test_the_wheel_and_the_identity_include_the_lsf_package_but_not_its_tests():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    wheel_config = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "lsf" in wheel_config["packages"] and "lsf/tests" in wheel_config["exclude"]
    assert "lsf" in HARNESS_PACKAGES and wheel.PACKAGES == HARNESS_PACKAGES
    assert project["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests", "lsf/tests"]


def copy_of_lsf(tmp_path):
    root = tmp_path / "lsf"
    shutil.copytree(Path(lsf.__file__).parent, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def test_changing_monitor_code_changes_the_harness_identity(tmp_path, monkeypatch):
    root = copy_of_lsf(tmp_path)
    real = config.importlib.util.find_spec

    def find_spec(name):
        if name == "lsf":
            return SimpleNamespace(submodule_search_locations=[str(root)])
        return real(name)

    monkeypatch.setattr(config.importlib.util, "find_spec", find_spec)
    before = implementation_identity()
    (root / "tests" / "test_packaging.py").write_text("# tests are not the harness\n")
    assert implementation_identity() == before
    with (root / "cost_monitor.py").open("a") as stream:
        stream.write("\n# a changed threshold would be here\n")
    assert implementation_identity() != before


def test_the_measurement_identity_covers_exactly_the_measurement_modules(tmp_path, monkeypatch):
    assert MEASUREMENT_MODULES == ("context.py", "cost_monitor.py", "cost_evidence.py")
    root = copy_of_lsf(tmp_path)
    monkeypatch.setattr(lsf, "__file__", str(root / "__init__.py"))
    before = measurement_identity()
    (root / "manager.py").write_text("# orchestration is not measurement\n")
    assert measurement_identity() == before
    (root / "context.py").write_text("# allocation interpretation changed\n")
    assert measurement_identity() != before
