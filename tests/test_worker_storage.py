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
        [sys.executable, "-P", "-c", 'import sys,qtb.cli; assert "qiskit" not in sys.modules'],
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
    assert a != quality_cache_key(
        {"id": "x"}, dict(case, options={"optimization_level": 3}), {}, {}, "h"
    )
    with pytest.raises(HarnessError):
        quality_cache_key({"id": "x"}, case, {}, {}, "h", mode="memory")
    assert register_decision(tmp_path, "m", "r1") == 0
    assert register_decision(tmp_path, "m", "r2") == 1
    assert register_decision(tmp_path, "m", "r1") == 0


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
