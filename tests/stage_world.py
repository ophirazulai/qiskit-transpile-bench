"""A stage world: sources, a store and fake stage work, for tests of the stage graph.

Every Qiskit build, compile and verifier call is replaced by fake work that writes the same
evidence the real work would. ``tests/conftest.py`` exposes it as the ``world`` fixture;
``lsf/tests`` runs the LSF job entry point and manager on top of it.
"""

import time
from pathlib import Path
from types import SimpleNamespace

from qtb.canonical import digest, read_json, write_json
from qtb.config import STAGES, implementation_identity, load_profile
from qtb.coordinator import Comparison
from qtb.coordinator.decide import decide
from qtb.coordinator.stages import read_state, run_compile, run_stage
from qtb.coordinator.storage import append_record, read_records
from qtb.errors import HarnessError
from qtb.evaluator import record

QUIET = lambda *_: None  # noqa: E731


def make_world(tmp_path, monkeypatch):
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
