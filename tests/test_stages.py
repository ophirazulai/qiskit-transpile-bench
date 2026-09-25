"""The stage commands end to end, with every Qiskit build, compile and verifier call mocked.

The fake work writes the same evidence the real work would, so these tests cover the stage
graph: session creation, locks, prerequisites, the gate, skips, retries, ``decide``, the
optional unit tests, ``clean`` and baseline reuse from the store.
"""

import copy
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from qtb.canonical import digest, file_hash, read_json, write_json
from qtb.cli import main
from qtb.config import STAGES, implementation_identity, load_profile
from qtb.coordinator import Comparison, worker_count
from qtb.coordinator.clean import clean
from qtb.coordinator.decide import decide
from qtb.coordinator.stages import read_state, run_compile, run_stage, skip_reason
from qtb.coordinator.storage import append_record, read_records
from qtb.errors import HarnessError, Precondition, Usage
from qtb.evaluator import record

QUIET = lambda *_: None  # noqa: E731


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Sources, a store and knobs that steer the fake work."""
    knobs = SimpleNamespace(
        ratio=0.97,
        candidate_correct=True,
        baseline_correct=True,
        c0="passed",
        audit="passed",
        upstream_regression=False,
        decisive=True,
        fail_once=set(),
        slow=0.0,
        calls=[],
    )
    store = tmp_path / "store"
    store.mkdir()
    sources = {}
    for name in ("main", "idea", "idea2"):
        sources[name] = tmp_path / "src" / name
        sources[name].mkdir(parents=True)
    monkeypatch.delenv("QTB_STORE", raising=False)
    _, policy, _ = load_profile("iterations-profile", verify=False)
    manifest, _, _ = load_profile("iterations-profile", verify=False)

    def maybe_fail(stage):
        knobs.calls.append(stage)
        if stage in knobs.fail_once:
            knobs.fail_once.discard(stage)
            raise HarnessError(f"injected {stage} failure")

    def build(self):
        maybe_fail("build")
        sources_ = self.run["sources"]
        key = digest({"baseline": sources_["baseline"]})
        entry = self.store.build_dir(key)
        reused = (entry / "READY").exists()
        if not reused:
            knobs.calls.append("baseline build")
            (entry / "source/test").mkdir(parents=True)
            (entry / "env/bin").mkdir(parents=True)
            (entry / "wheels").mkdir()
            baseline = {
                "id": digest(sources_["baseline"]),
                "python": str(entry / "env/bin/python"),
                "environment": str(entry / "env"),
                "wheel": str(entry / "wheels/qiskit.whl"),
                "snapshot": {"path": str(entry / "source"), "files": {}},
            }
            write_json(entry / "build.json", baseline)
            (entry / "READY").write_text("now\n")
        baseline = dict(read_json(entry / "build.json"), store_key=key, reused=reused)
        same = Path(sources_["baseline"]).name == Path(sources_["evolved"]).name
        evolved = dict(
            baseline if same else {**baseline, "id": digest(sources_["evolved"])},
            environment=str(self.directory / "builds/evolved-build/env"),
        )
        evolved.pop("store_key")
        evolved.pop("reused")
        self.run["builds"] = {"baseline": baseline, "evolved": evolved}
        self.run["scope"] = {
            str(level): {"stages": [], "components": [], "unmapped_paths": []}
            for level in range(4)
        }
        self.run["changed_paths"] = []
        self.run["hashes"].update(harness="harness", implementation=implementation_identity())
        (self.directory / "builds/evolved-build/env").mkdir(parents=True)
        (self.directory / "verifier").mkdir()
        self.save()

    def roundtrip(self, cases):
        self.evidence(record("harness/roundtrip", "harness", "passed"))

    def quality(self, cases):
        maybe_fail("quality")
        time.sleep(knobs.slow)
        saved = {(r["case_id"], r["revision"], r["seed"]) for r in read_records(
            self.directory / "observations.jsonl"
        )}
        builds = self.run["builds"]
        ratio = 1.0 if builds["baseline"]["id"] == builds["evolved"]["id"] else knobs.ratio
        for case in cases:
            reference = (
                case["circuit"]["sha256"]
                if case["semantic_reference"]["kind"] == "input"
                else case["semantic_reference"]["sha256"]
            )
            for seed in range(case["seeds_per_block"]):
                base = case.get("expected", {"D2": 100 + seed, "N2": 200 + seed})
                if case["role"] == "zero_baseline":
                    base = {"D2": 0, "N2": 0}
                if case["role"] == "deterministic":
                    base = {"D2": 100, "N2": 200}
                for revision in ("baseline", "evolved"):
                    if (case["case_id"], revision, seed) in saved:
                        continue
                    metrics = dict(base)
                    if revision == "evolved" and case["role"] in {"scored", "guard"}:
                        metrics = {k: v * ratio for k, v in metrics.items()}
                    append_record(
                        self.directory / "observations.jsonl",
                        {
                            "id": digest([case["case_id"], revision, seed]),
                            "case_id": case["case_id"],
                            "revision": revision,
                            "seed": seed,
                            "seed_block": "B0",
                            "mode": "quality",
                            "output_hash": "h",
                            "checks": [
                                {
                                    "oracle": "C6",
                                    "status": "verified",
                                    "reference_hash": reference,
                                    "input_domain": case["input_domain"],
                                    "covers": list(STAGES),
                                    "substituted": [],
                                }
                            ],
                            **metrics,
                        },
                    )
        return read_records(self.directory / "observations.jsonl")

    def audit(self, cases, rows):
        self.evidence(record("audit/determinism", "completeness", knobs.audit))

    def aggregate_checks(self, cases, rows):
        self.evidence(record("IA1/C0", "correctness", knobs.c0))
        self.evidence(record("IA1/C6", "correctness", "passed"))
        self.evidence(record("IA6/completeness", "completeness", "passed"))

    def behavior_checks(comparison, revisions):
        maybe_fail("correctness")
        knobs.calls.append(("behavior", revisions))
        for revision in revisions:
            subject = "reference" if revision == "baseline" else "evolved"
            good = knobs.baseline_correct if revision == "baseline" else knobs.candidate_correct
            append_record(
                comparison.directory / "correctness.jsonl",
                {"case_id": "c", "revision": revision, "seed": 0, "status": "verified"},
            )
            if not good:
                comparison.evidence(
                    record(f"behavior/{revision}/c/0", "correctness", "failed", subject)
                )
            comparison.evidence(record(f"IA1/C1-C5/{revision}", "correctness", "passed", subject))
        comparison.evidence(record("IA1/C1-C5", "correctness", "passed"))
        return knobs.decisive

    def clifford_checks(comparison, cases, revisions):
        time.sleep(knobs.slow)
        for revision in revisions:
            append_record(
                comparison.directory / "clifford.jsonl",
                {"case_id": "c", "revision": revision, "mode": "prefix", "status": "verified"},
            )
        both = {r["revision"] for r in read_records(comparison.directory / "clifford.jsonl")}
        comparison.evidence(
            record("IA1/C7", "correctness", "passed" if len(both) == 2 else "unresolved")
        )
        return True

    def upstream_checks(comparison):
        maybe_fail("unit-tests")
        time.sleep(knobs.slow)
        result = "failed" if knobs.upstream_regression else "passed"
        comparison.evidence(record("IA1/upstream/evolved", "correctness", result))
        comparison.evidence(record("IA1/upstream", "correctness", "passed"))

    def measure_costs(comparison):
        from qtb.coordinator.costs import required_cost_panels

        maybe_fail("cost")
        knobs.calls.append("measure costs")
        for name in required_cost_panels(comparison):
            comparison.evidence(record(f"IA5/{name}", "cost", "passed"))

    monkeypatch.setattr(Comparison, "build", build)
    monkeypatch.setattr(Comparison, "roundtrip", roundtrip)
    monkeypatch.setattr(Comparison, "roundtrip_cases", lambda self: [])
    monkeypatch.setattr(Comparison, "quality", quality)
    monkeypatch.setattr(Comparison, "audit", audit)
    monkeypatch.setattr(Comparison, "aggregate_checks", aggregate_checks)
    monkeypatch.setattr("qtb.coordinator.checks.behavior_checks", behavior_checks)
    monkeypatch.setattr("qtb.coordinator.checks.clifford_checks", clifford_checks)
    monkeypatch.setattr("qtb.coordinator.upstream.upstream_checks", upstream_checks)
    monkeypatch.setattr("qtb.coordinator.costs.measure_costs", measure_costs)
    # Cost bundles are fake, so the evidence is taken as measured.
    monkeypatch.setattr("qtb.coordinator.costs.replay_costs", lambda *args: args[-1])
    monkeypatch.setattr("qtb.coordinator.stages.host_mismatch", lambda build: None)
    return SimpleNamespace(
        knobs=knobs, store=store, sources=sources, root=tmp_path, manifest=manifest, policy=policy
    )


def compile_(world, name="s1", evolved="idea", baseline="main", **kwargs):
    root = world.root / "sessions" / name
    code = run_compile(
        world.sources[baseline],
        world.sources[evolved],
        kwargs.pop("profile", "iterations-profile"),
        root,
        kwargs.pop("store", world.store),
        progress=QUIET,
    )
    return root, code


def chain(root, stages=("quality", "correctness", "unit-tests", "cost")):
    return {stage: run_stage(root, stage, progress=QUIET) for stage in stages}


def verdict(root):
    code, decision = decide(root, progress=QUIET)
    return code, decision


def status(root, stage):
    state = read_state(root, stage)
    return state["status"] if state else None


def test_improving_session_passes_through_every_stage(world):
    root, code = compile_(world)
    assert code == 0
    assert chain(root) == {"quality": 0, "correctness": 0, "unit-tests": 0, "cost": 0}
    assert read_state(root, "quality")["gate"] == "improved"
    assert skip_reason(root, "cost") is None
    code, decision = verdict(root)
    assert (code, decision["status"]) == (0, "PASS")
    assert "harness/qualification" not in decision["required_ids"]
    assert "IA1/upstream" in decision["required_ids"]
    assert [row["status"] for row in decision["stages"]] == ["complete"] * 5
    assert decision["stage_state_hashes"]["cost"] == file_hash(root / "stages/cost/state.json")
    report = (root / "report.md").read_text()
    assert "## Stages" in report and "| cost | complete |" in report
    log = (root / "progress.log").read_text()
    assert "##### quality #####" in log and "Step durations (all stages)" in log
    # Each stage writes only its own evidence; decide merges them.
    merged = {r["id"] for r in read_json(root / "evidence.json")}
    assert {"harness/roundtrip", "IA1/C7", "IA1/stage-coverage", "IA5/timing"} <= merged
    state = read_state(root, "quality")
    assert state["format"] == "qtb-stage/1" and state["workers"] == worker_count()
    assert state["inputs"]["builds"]["baseline"] and state["machine"]["host"]


def test_no_improvement_closes_the_gate_and_skips_the_rest(world):
    world.knobs.ratio = 1.0
    world.knobs.candidate_correct = False  # never seen: correctness does not run
    root, _ = compile_(world)
    assert chain(root) == {"quality": 0, "correctness": 0, "unit-tests": 0, "cost": 0}
    assert [status(root, s) for s in ("correctness", "unit-tests", "cost")] == ["skipped"] * 3
    assert "gate closed" in read_state(root, "cost")["reason"]
    assert skip_reason(root, "cost").startswith("gate closed")
    assert "correctness" not in world.knobs.calls
    code, decision = verdict(root)
    assert (code, decision["status"]) == (10, "NO_IMPROVEMENT")
    notes = " ".join(decision["notes"])
    assert "not checked" in notes and "Upstream tests: not run" in notes
    assert "Baseline correctness: not known from the store" in notes


def test_quality_failure_closes_the_gate_as_a_violation(world):
    world.knobs.c0 = "failed"
    root, _ = compile_(world)
    chain(root)
    assert read_state(root, "quality")["gate"] == "closed"
    assert status(root, "correctness") == "skipped"
    assert verdict(root)[1]["status"] == "CONSTRAINT_VIOLATION"


@pytest.mark.parametrize("evolved", ["idea", "main"])
def test_unresolved_audit_closes_the_gate_also_on_aa(world, evolved):
    world.knobs.audit = "unresolved"
    root, _ = compile_(world, evolved=evolved)
    chain(root)
    assert read_state(root, "quality")["gate"] == "closed"
    assert status(root, "cost") == "skipped"
    assert verdict(root)[1]["status"] == "INCONCLUSIVE"


def test_aa_session_runs_every_stage(world):
    root, _ = compile_(world, evolved="main")
    chain(root)
    assert read_state(root, "quality")["gate"] == "aa"
    assert [status(root, s) for s in ("correctness", "unit-tests", "cost")] == ["complete"] * 3
    assert "measure costs" in world.knobs.calls
    assert verdict(root)[1]["status"] == "NO_IMPROVEMENT"


def test_correctness_failure_skips_cost(world):
    world.knobs.candidate_correct = False
    root, _ = compile_(world)
    chain(root, ("quality", "correctness"))
    # What tools/lsf/cost_if_gated.sh asks before choosing a host.
    assert skip_reason(root, "cost") == "correctness found a failure"
    chain(root, ("cost",))
    assert status(root, "cost") == "skipped"
    assert read_state(root, "cost")["reason"] == "correctness found a failure"
    assert "measure costs" not in world.knobs.calls
    assert verdict(root)[1]["status"] == "CONSTRAINT_VIOLATION"


def test_broken_baseline_sets_quality_aside(world):
    world.knobs.baseline_correct = False
    root, _ = compile_(world)
    chain(root)
    # The evolved revision is never checked against a broken baseline.
    assert ("behavior", ("evolved",)) not in world.knobs.calls
    assert status(root, "cost") == "skipped"
    decision = verdict(root)[1]
    assert decision["status"] == "INCONCLUSIVE"
    assert any("set aside" in note for note in decision["notes"])
    assert not any(r["id"].startswith("IA2/") and r["result"] == "passed"
                   for r in decision["constraints"])


def test_stages_refuse_to_run_before_their_prerequisites(world):
    with pytest.raises(Precondition, match="not a session"):
        run_stage(world.root / "sessions/none", "quality", progress=QUIET)
    root, _ = compile_(world)
    needs = {"correctness": "quality", "unit-tests": "quality", "cost": "correctness"}
    for stage, needed in needs.items():
        with pytest.raises(Precondition, match=f"needs {needed}"):
            run_stage(root, stage, progress=QUIET)
        assert main([stage, "--results-root", str(root)]) == 41
    run_stage(root, "quality", progress=QUIET)
    with pytest.raises(Precondition, match="needs correctness"):
        run_stage(root, "cost", progress=QUIET)


def test_decide_with_missing_stages_is_inconclusive_and_names_them(world):
    root, _ = compile_(world)
    run_stage(root, "quality", progress=QUIET)
    code, decision = verdict(root)
    assert (code, decision["status"]) == (30, "INCONCLUSIVE")
    assert any("correctness (not started)" in n for n in decision["notes"])
    rows = {row["stage"]: row for row in decision["stages"]}
    assert rows["correctness"]["status"] == "not started" and rows["correctness"]["required"]


def test_a_finished_stage_never_runs_again(world, capsys):
    root, _ = compile_(world)
    run_stage(root, "quality", progress=QUIET)
    before = file_hash(root / "stages/quality/state.json")
    calls = list(world.knobs.calls)
    assert main(["quality", "--results-root", str(root)]) == 0
    assert "already complete" in capsys.readouterr().out
    assert file_hash(root / "stages/quality/state.json") == before
    assert world.knobs.calls == calls


def test_a_failed_stage_resumes_and_drops_its_crash_record(world):
    root, _ = compile_(world)
    run_stage(root, "quality", progress=QUIET)
    world.knobs.fail_once.add("correctness")
    assert run_stage(root, "correctness", progress=QUIET) == 40
    assert status(root, "correctness") == "failed"
    decision = verdict(root)[1]
    assert decision["status"] == "ERROR"
    assert "harness/error/correctness" in {r["id"] for r in decision["constraints"]}
    assert run_stage(root, "correctness", progress=QUIET) == 0
    assert read_state(root, "correctness")["attempts"] == 2
    evidence = read_json(root / "stages/correctness/evidence.json")
    assert "harness/error/correctness" not in {r["id"] for r in evidence}
    # The retry restarted the suites: no duplicated rows.
    rows = read_records(root / "clifford.jsonl")
    assert sorted(r["revision"] for r in rows) == ["baseline", "evolved"]
    chain(root, ("unit-tests", "cost"))
    assert verdict(root)[1]["status"] == "PASS"


def test_a_killed_stage_resumes(world):
    root, _ = compile_(world)
    run_stage(root, "quality", progress=QUIET)
    state = read_state(root, "quality")
    write_json(root / "stages/quality/state.json", dict(state, status="running"))
    assert verdict(root)[1]["status"] == "INCONCLUSIVE"
    assert run_stage(root, "quality", progress=QUIET) == 0
    assert status(root, "quality") == "complete"


def test_compile_checks_its_session_directory(world, capsys):
    root, _ = compile_(world)
    with pytest.raises(Usage, match="other inputs"):
        compile_(world, evolved="idea2")
    with pytest.raises(Usage, match="other inputs"):
        compile_(world, profile="confirm-profile")
    busy = world.root / "sessions/other"
    busy.mkdir(parents=True)
    (busy / "notes.txt").write_text("mine")
    with pytest.raises(Usage, match="not a session"):
        compile_(world, name="other")
    code = main(
        [
            "compile", "--baseline", str(world.sources["main"]),
            "--evolved", str(world.sources["idea"]), "--store", str(world.store),
            "--results-root", str(root),
        ]
    )
    assert code == 0 and "already complete" in capsys.readouterr().out
    assert main(
        [
            "compile", "--baseline", str(world.sources["main"]),
            "--evolved", str(world.sources["idea2"]), "--store", str(world.store),
            "--results-root", str(root),
        ]
    ) == 64


def test_compile_resumes_its_unfinished_session(world):
    world.knobs.fail_once.add("build")
    root, code = compile_(world)
    assert code == 40 and status(root, "compile") == "failed"
    assert verdict(root)[1]["status"] == "ERROR"
    root, code = compile_(world)
    assert code == 0 and status(root, "compile") == "complete"
    assert read_state(root, "compile")["attempts"] == 2


def test_store_flag_wins_over_environment(world, monkeypatch, tmp_path):
    other = tmp_path / "other-store"
    other.mkdir()
    monkeypatch.setenv("QTB_STORE", str(other))
    root, _ = compile_(world)
    assert read_json(root / "run.json")["store"] == str(world.store.resolve())
    root, _ = compile_(world, name="s2", store=None)
    assert read_json(root / "run.json")["store"] == str(other.resolve())


@pytest.mark.parametrize(
    "unit_tests, regression, expected",
    [
        (None, False, "PASS"),
        ("complete", True, "CONSTRAINT_VIOLATION"),
        ("running", False, "INCONCLUSIVE"),
        ("failed", False, "ERROR"),
    ],
)
def test_optional_unit_tests_count_once_started(world, unit_tests, regression, expected):
    world.knobs.upstream_regression = regression
    root, _ = compile_(world)
    chain(root, ("quality", "correctness", "cost"))
    if unit_tests == "failed":
        world.knobs.fail_once.add("unit-tests")
    if unit_tests in {"complete", "failed"}:
        run_stage(root, "unit-tests", progress=QUIET)
    if unit_tests == "running":
        (root / "stages/unit-tests").mkdir(parents=True)
        write_json(root / "stages/unit-tests/state.json", {"status": "running"})
    decision = verdict(root)[1]
    assert decision["status"] == expected
    assert ("IA1/upstream" in decision["required_ids"]) is (unit_tests is not None)


def test_concurrent_stages_commit_separately_and_decide_ignores_partial_work(world):
    root, _ = compile_(world)
    run_stage(root, "quality", progress=QUIET)
    world.knobs.slow = 0.5
    codes = {}
    threads = [
        threading.Thread(target=lambda s=s: codes.__setitem__(s, run_stage(root, s, QUIET)))
        for s in ("correctness", "unit-tests")
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    during = verdict(root)[1]
    assert during["status"] == "INCONCLUSIVE"
    assert not any(r["id"].startswith("IA1/upstream") for r in during["constraints"])
    with pytest.raises(Precondition, match="already running"):
        run_stage(root, "unit-tests", progress=QUIET)
    for thread in threads:
        thread.join()
    assert codes == {"correctness": 0, "unit-tests": 0}
    world.knobs.slow = 0
    run_stage(root, "cost", progress=QUIET)
    decision = verdict(root)[1]
    assert decision["status"] == "PASS"
    ids = {r["id"] for r in decision["constraints"]}
    assert {"IA1/C7", "IA1/upstream/evolved", "IA1/C1-C5"} <= ids


def test_duplicate_record_ids_across_stages_are_a_harness_error(world):
    root, _ = compile_(world)
    chain(root)
    evidence = read_json(root / "stages/cost/evidence.json")
    duplicate = record("IA1/C7", "correctness", "passed")
    write_json(root / "stages/cost/evidence.json", [*evidence, duplicate])
    with pytest.raises(HarnessError, match="appears in both"):
        verdict(root)


def test_host_check_refuses_a_foreign_build(world, monkeypatch):
    from qtb.envbuild import host_mismatch

    monkeypatch.setattr("qtb.coordinator.stages.host_mismatch", host_mismatch)
    root, _ = compile_(world)
    import platform
    import sys

    run = read_json(root / "run.json")
    here = {
        "os": platform.system(), "architecture": platform.machine(), "python": sys.version,
    }
    run["builds"]["baseline"].update(identity=here, python=sys.executable)
    run["builds"]["evolved"].update(
        identity=dict(here, architecture="foreign"), python=sys.executable
    )
    write_json(root / "run.json", run)
    with pytest.raises(Precondition, match="cannot run the"):
        run_stage(root, "quality", progress=QUIET)


def test_workers_follow_the_lsf_grant(monkeypatch):
    monkeypatch.setenv("LSB_DJOB_NUMPROC", "3")
    assert worker_count() == 3
    monkeypatch.delenv("LSB_DJOB_NUMPROC")
    import os

    assert worker_count() == max(1, min(12, (os.cpu_count() or 2) - 1))


def test_clean_needs_a_current_decide_and_is_final(world):
    root, _ = compile_(world)
    chain(root, ("quality", "correctness", "cost"))
    evolved = root / "builds/evolved-build"
    (root / "jobs/j1/scratch").mkdir(parents=True)
    (root / "upstream-baseline/cargo/registry").mkdir(parents=True)
    (root / "jobs/j1/job.json").write_text("{}")
    store_before = sorted(str(p) for p in world.store.rglob("*"))
    with pytest.raises(Precondition, match="Run decide"):
        clean(root, progress=QUIET)
    code, first = verdict(root)
    # Optional unit tests started after decide: that verdict is stale.
    run_stage(root, "unit-tests", progress=QUIET)
    with pytest.raises(Precondition, match="unit-tests"):
        clean(root, progress=QUIET)
    code, first = verdict(root)
    assert clean(root, progress=QUIET) == 0
    assert not evolved.exists() and not (root / "verifier").exists()
    assert not (root / "jobs/j1/scratch").exists() and (root / "jobs/j1/job.json").exists()
    assert not (root / "upstream-baseline/cargo").exists()
    record_ = read_json(root / "clean.json")
    assert record_["status"] == "complete"
    assert str(evolved) in record_["planned"]
    assert sorted(str(p) for p in world.store.rglob("*")) == store_before
    for stage in ("quality", "correctness", "unit-tests", "cost"):
        assert main([stage, "--results-root", str(root)]) == 41
    again = verdict(root)[1]
    assert again["status"] == first["status"] == "PASS"
    assert clean(root, progress=QUIET) == 0  # already clean


def test_an_interrupted_clean_is_resumed(world):
    root, _ = compile_(world)
    chain(root)
    verdict(root)
    (root / "verifier-cache/ab").mkdir(parents=True)
    write_json(
        root / "clean.json",
        {
            "status": "cleaning",
            "planned": {
                str(root / "verifier-cache"): {"kind": "directory", "bytes": 5, "files": 0},
            },
        },
    )
    with pytest.raises(Precondition, match="interrupted"):
        run_stage(root, "quality", progress=QUIET)
    assert clean(root, progress=QUIET) == 0
    assert not (root / "verifier-cache").exists()
    assert read_json(root / "clean.json")["status"] == "complete"


def test_clean_is_refused_while_a_stage_is_running(world):
    root, _ = compile_(world)
    chain(root)
    verdict(root)
    state = read_state(root, "cost")
    write_json(root / "stages/cost/state.json", dict(state, status="running"))
    with pytest.raises(Precondition, match="did not finish"):
        clean(root, progress=QUIET)


def test_second_session_reuses_the_stored_baseline(world):
    first, _ = compile_(world)
    chain(first)
    assert world.knobs.calls.count("baseline build") == 1
    stored = list((world.store / "correctness").iterdir())
    assert len(stored) == 1
    entry_evidence = read_json(stored[0] / "evidence.json")
    assert {r["id"] for r in entry_evidence} == {
        "IA1/C1-C5/baseline", "baseline/preflight"
    }
    # No evolved data goes into the store.
    for path in world.store.rglob("*.json*"):
        text = path.read_text()
        assert '"evolved"' not in text and "IA1/C7" not in text, path

    second, _ = compile_(world, name="s2", evolved="idea2")
    assert world.knobs.calls.count("baseline build") == 1
    assert read_state(second, "compile")["reused"]["baseline_build"]
    world.knobs.calls.clear()
    chain(second)
    assert ("behavior", ("baseline",)) not in world.knobs.calls
    assert ("behavior", ("evolved",)) in world.knobs.calls
    rows = read_records(second / "correctness.jsonl")
    assert [r.get("cached_from") is not None for r in rows] == [True, False]
    evidence = read_json(second / "stages/correctness/evidence.json")
    assert next(r for r in evidence if r["id"] == "baseline/preflight")["cached_from"]
    decision = verdict(second)[1]
    assert decision["status"] == "PASS"
    assert any("baseline correctness: from the store" in n for n in decision["notes"])

    # Cleaning the first session leaves the second one's stored baseline intact.
    verdict(first)
    clean(first, progress=QUIET)
    key = read_json(second / "run.json")["builds"]["baseline"]["store_key"]
    assert (world.store / "builds" / key / "READY").exists()


def test_an_indecisive_baseline_correctness_result_is_never_stored(world):
    world.knobs.decisive = False
    root, _ = compile_(world)
    chain(root)
    assert status(root, "correctness") == "complete"
    assert not (world.store / "correctness").exists()


def test_aa_bypasses_stored_baseline_results(world):
    first, _ = compile_(world)
    chain(first)
    world.knobs.calls.clear()
    aa, _ = compile_(world, name="aa", evolved="main")
    chain(aa)
    assert ("behavior", ("baseline",)) in world.knobs.calls
    rows = read_records(aa / "correctness.jsonl")
    assert not any(r.get("cached_from") for r in rows)


def test_a_broken_baseline_is_reported_by_later_gate_closed_sessions(world):
    world.knobs.baseline_correct = False
    first, _ = compile_(world)
    chain(first)
    world.knobs.ratio = 1.0
    second, _ = compile_(world, name="s2", evolved="idea2")
    chain(second)
    decision = verdict(second)[1]
    assert decision["status"] == "NO_IMPROVEMENT"
    assert any("The baseline is broken" in note for note in decision["notes"])


def test_a_missing_store_entry_stops_later_stages(world):
    import shutil

    root, _ = compile_(world)
    key = read_json(root / "run.json")["builds"]["baseline"]["store_key"]
    shutil.rmtree(world.store / "builds" / key)
    with pytest.raises(Precondition, match="missing from the store"):
        run_stage(root, "quality", progress=QUIET)
    assert verdict(root)[1]["status"] == "INCONCLUSIVE"
    # The next session rebuilds it.
    compile_(world, name="s2")
    assert world.knobs.calls.count("baseline build") == 2


def test_version_five_policies_need_no_qualification():
    for profile in ("iterations-profile", "confirm-profile"):
        manifest, policy, _ = load_profile(profile, verify=False)
        assert policy["version"] == manifest["version"] == 5
        assert "harness/qualification" not in policy["required_ids"]
        assert not any(i.endswith("1/upstream") for i in policy["required_ids"])
        assert "qualification" not in policy and "status" not in manifest


def test_decide_works_from_the_archived_profile(world, monkeypatch):
    root, _ = compile_(world)
    chain(root)
    archived = copy.deepcopy(read_json(root / "policy.json"))
    changed = dict(archived, rng_seed=archived["rng_seed"] + 1)
    write_json(root / "policy.json", changed)
    with pytest.raises(HarnessError, match="policy hash mismatch"):
        verdict(root)
    write_json(root / "policy.json", archived)
    assert verdict(root)[1]["status"] == "PASS"
