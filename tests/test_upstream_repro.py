import shutil
import sys
import zipfile
from pathlib import Path

from qtb.canonical import CIRCUIT_FORMAT, digest, file_hash, read_json, write_circuit, write_json
from qtb.config import case_hash
from qtb.coordinator.upstream import derive_exclusions
from qtb.repro import export_reproducer


def test_seed_reshuffle_proposes_only_new_failures_from_baseline_tests(tmp_path, monkeypatch):
    source = tmp_path / "baseline"
    test = source / "test/python/transpiler/test_outputs.py"
    test.parent.mkdir(parents=True)
    (source / "test/python/compiler").mkdir()
    (source / "qiskit").mkdir()
    (source / "qiskit/__init__.py").write_text('raise RuntimeError("source tree was imported")')
    test.write_text(
        "from qiskit.transpiler.passes import SabreSwap\n"
        "from qiskit.transpiler import CouplingMap\n"
        "def test_output_pin():\n"
        "    assert SabreSwap(CouplingMap.from_line(2), seed=11).seed == 11\n"
        "def test_preexisting():\n    assert False\n"
    )
    run = tmp_path / "run"
    write_json(
        run / "run.json",
        dict(
            builds={
                "baseline": dict(
                    id="baseline", python=sys.executable, snapshot={"path": str(source)}
                )
            }
        ),
    )
    monkeypatch.setattr("qtb.coordinator.upstream.run_logged", lambda *args, **kwargs: None)
    result = derive_exclusions(run, tmp_path)
    assert result["status"] == "unreviewed"
    assert result["output_pinned_tests"] == [
        "test/python/transpiler/test_outputs.py::test_output_pin"
    ]
    assert result["preexisting_failures"] == [
        "test/python/transpiler/test_outputs.py::test_preexisting"
    ]
    assert result["ordinary"]["result"] == "failed"


def test_reproducer_uses_archived_wheels_fixtures_and_dependency_lock(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    for name in ("input.gz", "target.json", "reference.gz"):
        (fixtures / name).write_bytes(name.encode())
    revision = tmp_path / "qiskit-test.whl"
    revision.write_bytes(b"revision")
    run = tmp_path / "run"
    (run / "harness-wheel").mkdir(parents=True)
    with zipfile.ZipFile(run / "harness-wheel/qtb.whl", "w") as archive:
        archive.writestr("qtb/data/envs/common.lock", "archived-dependency==1\n")
    case = dict(
        circuit={"file": "input.gz"},
        target={"file": "target.json"},
        semantic_reference={"kind": "frozen_circuit", "file": "reference.gz"},
    )
    job = run / "job.json"
    write_json(
        job,
        dict(case=case, seeds=[0, 1], fixture_root=str(fixtures), build={"wheel": str(revision)}),
    )
    observation = dict(seed=1, worker={"job_file": str(job)})
    output = export_reproducer(observation, run, tmp_path / "repro")
    assert read_json(output / "job.json")["seeds"] == [1]
    assert (output / "common.lock").read_text() == "archived-dependency==1\n"
    assert (output / "fixtures/reference.gz").read_bytes() == b"reference.gz"
    assert {p.name for p in (output / "wheels").iterdir()} == {"qtb.whl", "qiskit-test.whl"}
    compile((output / "run.py").read_text(), str(output / "run.py"), "exec")


def test_reproducer_survives_relocated_run_and_missing_original_fixtures(tmp_path):
    old_root = tmp_path / "original"
    old_run = old_root / "runs" / "run-1"
    old_run.mkdir(parents=True)
    old_fixtures = tmp_path / "source-fixtures"
    old_fixtures.mkdir()
    circuit = old_fixtures / "input.gz"
    circuit_hash = write_circuit(circuit, {"format": CIRCUIT_FORMAT}, [])
    circuit_bytes = circuit.read_bytes()
    target = old_fixtures / "target.json"
    write_json(target, {"format": "qtb-target/1"})
    case = {
        "case_id": "example",
        "circuit": {"file": circuit.name, "sha256": circuit_hash},
        "target": {"file": target.name, "sha256": digest(read_json(target))},
        "semantic_reference": {"kind": "input"},
        "timeout_s": 30,
    }
    harness = old_run / "harness-wheel/harness.whl"
    harness.parent.mkdir()
    with zipfile.ZipFile(harness, "w") as archive:
        archive.writestr("qtb/data/envs/common.lock", "archived-dependency==1\n")
        for artifact in (circuit, target):
            archive.write(artifact, "qtb/data/fixtures/" + artifact.name)
    revision = old_run / "builds/baseline-build/wheels/qiskit.whl"
    revision.parent.mkdir(parents=True)
    revision.write_bytes(b"archived revision")
    build = {"id": "build-1", "wheel": str(revision), "wheel_sha256": file_hash(revision)}
    job_path = old_run / "jobs/first/job.json"
    write_json(
        job_path,
        {
            "protocol": "qtb-worker/1",
            "mode": "quality",
            "case": case,
            "seeds": [0, 1],
            "fixture_root": str(old_fixtures),
            "build": build,
        },
    )
    write_json(
        old_run / "run.json",
        {"run_id": "run-1", "results_root": str(old_root), "builds": {"baseline": build}},
    )
    write_json(old_run / "manifest.json", {"cases": [case]})
    observation = {
        "seed": 1,
        "mode": "quality",
        "case_id": "example",
        "case_hash": case_hash(case),
        "revision": "baseline",
        "build_id": build["id"],
        "worker": {"job_file": str(job_path), "status": "ok", "output_hash": "recorded"},
    }
    new_run = tmp_path / "archive/run-1"
    new_run.parent.mkdir()
    shutil.move(old_run, new_run)
    shutil.rmtree(old_fixtures)
    output = export_reproducer(observation, new_run, tmp_path / "repro-relocated")
    assert file_hash(output / "wheels/qiskit.whl") == build["wheel_sha256"]
    assert (output / "fixtures/input.gz").read_bytes() == circuit_bytes
    assert read_json(output / "job.json")["seeds"] == [1]
    assert Path(read_json(output / "job.json")["build"]["wheel"]).is_file()

    # A reused quality observation can point to a deleted source run. Its
    # exact case and build identities are enough to reconstruct the job.
    shutil.rmtree(new_run / "jobs")
    cached = export_reproducer(observation, new_run, tmp_path / "repro-cached")
    assert read_json(cached / "job.json")["case"]["case_id"] == "example"
    assert (cached / "fixtures/target.json").is_file()
