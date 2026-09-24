"""A regular installed package can supply an isolated worker/verifier wheel."""

import csv
import io
import os
import subprocess
import sys
import zipfile
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from qtb.canonical import file_hash
from qtb.coordinator import wheel as installed_wheel
from qtb.errors import HarnessError


class InstalledDistribution:
    version = "0.1.0"
    metadata = {"Name": "qiskit-transpile-bench"}

    def __init__(self, root):
        self.root = root
        self.files = [
            PurePosixPath(path.relative_to(root).as_posix())
            for path in root.rglob("*")
            if path.is_file()
        ]

    def locate_file(self, path):
        return self.root / path

    def read_text(self, name):
        return ""


def installed_tree(tmp_path, monkeypatch):
    root = tmp_path / "site-packages"
    for package in installed_wheel.PACKAGES:
        path = root / package
        path.mkdir(parents=True)
        (path / "__init__.py").write_text(f"NAME = {package!r}\n")
    for profile in ("iterations-profile", "confirm-profile"):
        path = root / "qtb/data/profiles" / profile
        path.mkdir(parents=True)
        (path / "manifest.json").write_text("{}\n")
    info = root / "qiskit_transpile_bench-0.1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.3\nName: qiskit-transpile-bench\nVersion: 0.1.0\n"
    )
    (info / "WHEEL").write_text("Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
    (info / "entry_points.txt").write_text(
        "[console_scripts]\nqiskit-transpile-bench = qtb:main\n"
    )
    distribution = InstalledDistribution(root)
    monkeypatch.setattr(installed_wheel.importlib.metadata, "distribution", lambda *_: distribution)
    monkeypatch.setattr(
        installed_wheel.importlib.util,
        "find_spec",
        lambda package: SimpleNamespace(submodule_search_locations=[str(root / package)]),
    )
    return root


def test_repacked_installed_wheel_is_deterministic_and_installable(tmp_path, monkeypatch):
    installed_tree(tmp_path, monkeypatch)
    first = installed_wheel.repack_installed_harness(tmp_path / "wheels")
    second = installed_wheel.repack_installed_harness(tmp_path / "other")
    assert file_hash(first) == file_hash(second)
    with zipfile.ZipFile(first) as archive:
        names = set(archive.namelist())
        assert "qtb/__init__.py" in names
        assert "qtb_worker/__init__.py" in names
        assert "qtb_verifier/__init__.py" in names
        record = archive.read("qiskit_transpile_bench-0.1.0.dist-info/RECORD").decode()
        rows = list(csv.reader(io.StringIO(record)))
        assert len(rows) == len(names)
        assert all(row[1].startswith("sha256=") for row in rows[:-1])
    installed = tmp_path / "target"
    with zipfile.ZipFile(first) as archive:
        archive.extractall(installed)
    proc = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            "import qtb, qtb_worker, qtb_verifier; "
            "assert qtb.__file__.startswith(__import__('sys').argv[1])",
            str(installed),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ, PYTHONPATH=str(installed)),
    )
    assert proc.returncode == 0, proc.stderr
    assert (installed / "qtb/data/profiles/confirm-profile/manifest.json").exists()


def test_repack_rejects_import_shadowing(tmp_path, monkeypatch):
    installed_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(
        installed_wheel.importlib.util,
        "find_spec",
        lambda _package: SimpleNamespace(submodule_search_locations=[str(tmp_path / "other")]),
    )
    with pytest.raises(HarnessError, match="differs from installed distribution"):
        installed_wheel.repack_installed_harness(tmp_path / "wheels")


def test_repack_rejects_editable_install_without_source(tmp_path, monkeypatch):
    root = installed_tree(tmp_path, monkeypatch)
    distribution = InstalledDistribution(root)
    distribution.read_text = lambda _name: '{"dir_info": {"editable": true}}'
    monkeypatch.setattr(installed_wheel.importlib.metadata, "distribution", lambda *_: distribution)
    with pytest.raises(HarnessError, match="editable harness install"):
        installed_wheel.repack_installed_harness(tmp_path / "wheels")
