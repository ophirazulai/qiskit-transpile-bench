import sys
import zipfile

from qtb.canonical import read_json, write_json
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
