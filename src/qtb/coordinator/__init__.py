"""Comparison orchestration; this process never imports Qiskit."""

import random
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from qtb.canonical import (
    circuit_hash,
    circuit_lines,
    digest,
    file_hash,
    read_circuit,
    read_json,
    verify_artifact,
    write_json,
)
from qtb.config import STAGES, case_hash, data_root, load_profile
from qtb.coordinator.process import run_worker
from qtb.coordinator.storage import (
    append_record,
    quality_cache_key,
    read_records,
    register_decision,
)
from qtb.envbuild import (
    baseline_toolchain,
    build_revision,
    diff_snapshots,
    machine_identity,
    run_logged,
    sanitized_environment,
    snapshot,
)
from qtb.errors import HarnessError, Incomplete
from qtb.evaluator import evaluate_quality, record
from qtb.evaluator.scope import changed_scope, covered
from qtb.metrics import StructuralChecker, layout_errors
from qtb.metrics.replay import replay
from qtb.reporter import make_decision, write_report


def structural_result(path, target, layout, input_width, initial_layout=None):
    lines = circuit_lines(path)
    header = next(lines)
    checker = StructuralChecker(header, target)
    for operation in lines:
        checker.consume(operation)
    checker.errors.extend(layout_errors(layout, input_width, header["num_qubits"], initial_layout))
    return checker.result()


class Comparison:
    def __init__(
        self,
        baseline,
        evolved,
        profile="iterations-profile",
        results_root="results",
        resume=None,
        change_scope=(),
        progress=print,
    ):
        self.root = Path(results_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.progress = progress
        self.data = data_root()
        self.fixtures = self.data / "fixtures"
        self.manifest, self.policy, hashes = load_profile(profile, self.data, verify=False)
        self.prefix = "CA" if profile == "confirm-profile" else "IA"
        self.records = []
        self.declaration = change_scope
        if resume:
            self.directory = Path(resume).resolve()
            self.run = read_json(self.directory / "run.json")
            if self.run["hashes"]["manifest"] != hashes["manifest"]:
                raise HarnessError("Resumption requires the original manifest")
            if self.run["hashes"]["policy"] != hashes["policy"]:
                raise HarnessError("Resumption requires the original policy")
            self.records = (
                read_json(self.directory / "evidence.json")
                if (self.directory / "evidence.json").exists()
                else []
            )
        else:
            run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
            self.directory = self.root / "runs" / run_id
            self.directory.mkdir(parents=True)
            self.run = {
                "run_id": run_id,
                "profile": profile,
                "hashes": hashes,
                "created_at": datetime.now(UTC).isoformat(),
                "results_root": str(self.root),
                "machine": machine_identity(),
                "builds": {},
                "status": "created",
                "sources": {
                    "baseline": str(Path(baseline).resolve()),
                    "evolved": str(Path(evolved or baseline).resolve()),
                },
            }
            write_json(self.directory / "manifest.json", self.manifest)
            write_json(self.directory / "policy.json", self.policy)
            self.save()

    def save(self):
        write_json(self.directory / "run.json", self.run)

    def evidence(self, row):
        # Repeated phases replace an earlier unresolved aggregate, never duplicate IDs.
        self.records = [r for r in self.records if r["id"] != row["id"]] + [row]
        write_json(self.directory / "evidence.json", self.records)

    def build(self, need_control=True):
        load_profile(self.run["profile"], self.data, verify=True)
        build_root = self.directory / "builds"
        build_root.mkdir(exist_ok=True)
        snapshots = {}
        for revision, source in self.run["sources"].items():
            path = build_root / f"{revision}.snapshot.json"
            snapshots[revision] = (
                read_json(path) if path.exists() else snapshot(source, build_root / revision)
            )
        self.run["changed_paths"] = diff_snapshots(snapshots["baseline"], snapshots["evolved"])
        self.run["scope"] = {
            str(level): changed_scope(self.run["changed_paths"], level, self.declaration)
            for level in range(4)
        }
        self.progress(f"Scope: {self.run['scope']}")
        # Build the harness wheel once; its content hash enters observation cache keys.
        wheels = self.directory / "harness-wheel"
        wheels.mkdir(exist_ok=True)
        found = list(wheels.glob("*.whl"))
        if not found:
            project = Path(__file__).resolve().parents[3]
            if not (project / "pyproject.toml").exists():
                raise HarnessError("A source checkout is required to build the harness wheel")
            command = ["uv", "build", "--wheel", "--out-dir", str(wheels), str(project)]
            run_logged(
                command,
                self.directory,
                sanitized_environment(),
                self.directory / "harness-build.log",
            )
            found = list(wheels.glob("*.whl"))
        if len(found) != 1:
            raise HarnessError("Expected exactly one harness wheel")
        self.run["hashes"]["harness"] = file_hash(found[0])
        toolchain = baseline_toolchain(snapshots["baseline"])
        for revision in ["baseline", "evolved"] + (["control"] if need_control else []):
            directory = build_root / f"{revision}-build"
            if (directory / "build.json").exists():
                build = read_json(directory / "build.json")
            else:
                self.progress(f"Building {revision} with baseline Rust toolchain {toolchain}.")
                build = build_revision(
                    snapshots["baseline" if revision == "control" else revision],
                    directory,
                    self.data / "envs",
                    found[0],
                    toolchain,
                )
            self.run["builds"][revision] = build
            self.save()
        self.build_verifier(found[0])
        self.run["status"] = "built"
        self.save()

    def build_verifier(self, wheel):
        directory = self.directory / "verifier"
        python = directory / "env/bin/python"
        if not (directory / "ready.json").exists():
            directory.mkdir(exist_ok=True)
            env, log = sanitized_environment(), directory / "build.log"
            run_logged([sys.executable, "-m", "venv", directory / "env"], directory, env, log)
            run_logged(
                [
                    python,
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    self.data / "envs/common.lock",
                    "-r",
                    self.data / "envs/verifier.lock",
                    wheel,
                ],
                directory,
                env,
                log,
            )
            write_json(directory / "ready.json", {"harness": file_hash(wheel)})
        self.run["verifier_python"] = str(python)

    def job(self, revision, case, mode, seeds, edits=(), hash_seed="0"):
        directory = self.directory / "jobs" / uuid.uuid4().hex
        job = {
            "mode": mode,
            "case": case,
            "seeds": list(seeds),
            "fixture_root": str(self.fixtures),
            "timeout_s": case["timeout_s"],
            "pipeline_edits": list(edits),
            "bindings": case.get("bindings", []),
        }
        result = run_worker(self.run["builds"][revision], job, directory, hash_seed)
        for row in result:
            row["job_file"] = str(directory / "job.json")
        return result

    def roundtrip(self, cases):
        seen = set()
        for revision in ("baseline", "evolved"):
            for case in cases:
                key = revision, case["circuit"]["sha256"], case["target"]["sha256"]
                if key in seen:
                    continue
                seen.add(key)
                row = self.job(revision, case, "roundtrip", [0])[0]
                if (
                    row["status"] != "ok"
                    or row.get("circuit_hash") != key[1]
                    or row.get("target_hash") != key[2]
                ):
                    raise HarnessError(
                        f"Input roundtrip failed on {revision}: {case['case_id']}: {row}"
                    )
        self.evidence(record("harness/roundtrip", "harness", "passed"))

    def oracle(self, case, result, oracle, reference=None, **extra):
        ref = reference or (
            case["semantic_reference"]
            if case["semantic_reference"]["kind"] == "frozen_circuit"
            else case["circuit"]
        )
        reference_path = verify_artifact(self.fixtures, ref, circuit=True)
        directory = self.directory / "oracle-jobs" / uuid.uuid4().hex
        directory.mkdir(parents=True)
        job = {
            "protocol": "qtb-verifier/1",
            "reference": str(reference_path),
            "reference_hash": ref["sha256"],
            "output": result["output"],
            "layout": result["layout"],
            "input_domain": case["input_domain"],
            "oracle": oracle,
            **extra,
        }
        write_json(directory / "job.json", job)
        command = [
            self.run["verifier_python"],
            "-P",
            "-m",
            "qtb_verifier",
            "--job",
            str(directory / "job.json"),
            "--out",
            str(directory / "result.json"),
        ]
        try:
            run_logged(
                command, directory, sanitized_environment(), directory / "verifier.log", timeout=300
            )
            return read_json(directory / "result.json")
        except HarnessError as exc:
            return {"status": "unverified", "oracle": oracle, "detail": str(exc)}

    def quality(self, cases, smoke=False, block="B0", revisions=("baseline", "evolved")):
        offsets = {"B0": 0, "KB1": 100, "KB2": 200}
        saved = read_records(self.directory / "observations.jsonl")
        completed = {(r["case_id"], r["revision"], r["seed"], r["seed_block"]) for r in saved}
        for revision in revisions:
            for index, case in enumerate(cases):
                self.progress(f"{revision}: {case['case_id']} ({index + 1}/{len(cases)}, {block})")
                target = read_json(self.fixtures / case["target"]["file"])
                count = 1 if smoke else case["seeds_per_block"]
                seeds = [
                    s
                    for s in range(offsets[block], offsets[block] + count)
                    if (case["case_id"], revision, s, block) not in completed
                ]
                key = quality_cache_key(
                    self.run["builds"][revision],
                    case,
                    self.run["machine"],
                    self.policy["measurement_protocol"],
                    self.run["hashes"]["harness"],
                    block,
                )
                cache = self.root / "quality-cache" / key
                if not smoke:
                    remaining = []
                    for seed in seeds:
                        path = cache / f"{seed}.json"
                        if path.exists():
                            cached = read_json(path)
                            if Path(cached["output"]).exists():
                                cached.update(revision=revision, cached=True)
                                append_record(self.directory / "observations.jsonl", cached)
                                continue
                        remaining.append(seed)
                    seeds = remaining
                for start in range(0, len(seeds), 25):
                    batch = seeds[start : start + 25]
                    rows = self.job(revision, case, "quality", batch)
                    for result in rows:
                        observation = {
                            "format": "qtb-observation/1",
                            "case_id": case["case_id"],
                            "case_hash": case_hash(case),
                            "revision": revision,
                            "build_id": self.run["builds"][revision]["id"],
                            "seed": result["seed"],
                            "seed_block": block,
                            "mode": "quality",
                            "worker": result,
                            "circuit_hash": case["circuit"]["sha256"],
                            "reference_hash": case["semantic_reference"].get(
                                "sha256", case["circuit"]["sha256"]
                            ),
                            "input_domain": case["input_domain"],
                            "target_hash": case["target"]["sha256"],
                            "options": case["options"],
                            "checks": [],
                        }
                        observation["id"] = digest(
                            {
                                k: observation[k]
                                for k in ("case_hash", "build_id", "revision", "seed", "seed_block")
                            }
                        )
                        if result["status"] == "ok":
                            if circuit_hash(result["output"]) != result["output_hash"]:
                                raise HarnessError("Exported output hash mismatch")
                            structural = structural_result(
                                result["output"],
                                target,
                                result["layout"],
                                case["logical_qubits"],
                                case["options"].get("initial_layout"),
                            )
                            observation.update(
                                {k: structural[k] for k in ("D2", "N2") if k in structural}
                            )
                            observation.update(
                                output=result["output"],
                                output_hash=result["output_hash"],
                                layout=result["layout"],
                                fingerprint=result.get("fingerprint", {}),
                            )
                            observation["checks"].append(dict(structural, oracle="C0"))
                            if not smoke and structural["status"] == "verified":
                                self.check_routing(revision, case, observation)
                                if (
                                    self.run["profile"] == "confirm-profile"
                                    and case["role"] == "scored"
                                    and case["size_band"] == "small"
                                    and result["seed"] < 10
                                    and not observation.get("free_parameters")
                                ):
                                    observation["checks"].append(
                                        self.oracle(case, result, "C1-lite")
                                    )
                        else:
                            subject = "reference" if revision == "baseline" else "evolved"
                            self.evidence(
                                record(
                                    f"failure/{observation['id']}",
                                    "completeness",
                                    "failed",
                                    subject,
                                    detail=result.get("error", "Worker failed"),
                                )
                            )
                        append_record(self.directory / "observations.jsonl", observation)
                        if result["status"] == "ok" and not smoke:
                            write_json(cache / f"{result['seed']}.json", observation)
        return read_records(self.directory / "observations.jsonl")

    def check_routing(self, revision, case, observation):
        seed = observation["seed"]
        if case["role"] not in {"scored", "guard"} and seed % 100 >= 10:
            return
        initial = self.job(
            revision, case, "prefix", [seed], [f"drop_stage:{s}" for s in STAGES[1:]]
        )[0]
        routed = self.job(
            revision, case, "prefix", [seed], [f"drop_stage:{s}" for s in STAGES[3:]]
        )[0]
        if initial["status"] != "ok" or routed["status"] != "ok":
            observation["checks"].append(
                {"oracle": "C6", "status": "unverified", "detail": "Prefix compilation failed"}
            )
            return
        h0, ops0 = read_circuit(initial["output"])
        h1, ops1 = read_circuit(routed["output"])
        elided = initial["layout"]["final_index_layout"] if initial["layout"] else None
        checked = replay(ops0, ops1, routed["layout"], h0["num_qubits"], h1["num_qubits"], elided)
        if case["optimization_level"] != 3 and observation["layout"] != routed["layout"]:
            checked.update(status="mismatch", detail="Full and routing-prefix layouts differ")
        checked.update(
            oracle="C6",
            reference_hash=observation["reference_hash"],
            input_domain=case["input_domain"],
            prefix_jobs=[initial["job_file"], routed["job_file"]],
        )
        observation["checks"].append(checked)

    def audit(self, cases, observations):
        by_case = {c["case_id"]: c for c in cases}
        rng = random.Random(self.policy["rng_seed"])
        ok = True
        for revision in ("baseline", "evolved"):
            rows = [
                r
                for r in observations
                if r["revision"] == revision and r.get("output_hash") and r["seed_block"] == "B0"
            ]
            count = min(len(rows), max(10, int(len(rows) * 0.05 + 0.999)))
            if not rows:
                ok = False
            for i, row in enumerate(rng.sample(rows, count)):
                result = self.job(
                    revision,
                    by_case[row["case_id"]],
                    "quality",
                    [row["seed"]],
                    hash_seed="1" if i % 2 else "0",
                )[0]
                if (
                    result["status"] != "ok"
                    or result.get("output_hash") != row["output_hash"]
                    or result.get("layout") != row["layout"]
                ):
                    ok = False
        self.evidence(record("audit/determinism", "completeness", "passed" if ok else "unresolved"))
        if not ok:
            # A cache namespace cannot remain reusable after a failed determinism audit.
            for path in (self.root / "quality-cache").glob("*/[0-9]*.json"):
                row = read_json(path)
                if row["build_id"] in {b["id"] for b in self.run["builds"].values()}:
                    path.unlink()

    def aggregate_checks(self, cases, observations):
        quality = [r for r in observations if r["seed_block"] == "B0"]
        expected = sum(c["seeds_per_block"] for c in cases) * 2
        self.evidence(
            record(
                f"{self.prefix}6/completeness",
                "completeness",
                "passed" if len(quality) == expected else "unresolved",
            )
        )
        for oracle in ("C0", "C6"):
            checks = [c for row in quality for c in row["checks"] if c["oracle"] == oracle]
            for revision, subject in (("baseline", "reference"), ("evolved", "evolved")):
                failures = [
                    row["id"]
                    for row in quality
                    if row["revision"] == revision
                    for c in row["checks"]
                    if c["oracle"] == oracle and c["status"] == "mismatch"
                ]
                if failures:
                    self.evidence(
                        record(
                            f"{self.prefix}1/{oracle}/{revision}",
                            "correctness",
                            "failed",
                            subject,
                            observations=failures,
                        )
                    )
            status = (
                "passed"
                if len(checks) == expected and all(c["status"] == "verified" for c in checks)
                else "unresolved"
            )
            self.evidence(record(f"{self.prefix}1/{oracle}", "correctness", status))
        by_case = {c["case_id"]: c for c in cases}
        coverage = []
        for case in cases:
            if case["role"] != "scored":
                continue
            checks = [
                c
                for r in quality
                if r["case_id"] == case["case_id"] and r["revision"] == "evolved"
                for c in r["checks"]
            ]
            scope = self.run["scope"][str(case["optimization_level"])]
            if not covered(by_case[case["case_id"]], checks, scope):
                coverage.append(case["case_id"])
        self.evidence(
            record(
                f"{self.prefix}1/stage-coverage",
                "correctness",
                "unresolved" if coverage else "passed",
                cases=coverage,
            )
        )

    def finish(self):
        rows = read_records(self.directory / "observations.jsonl")
        self.run["decisions_before"] = register_decision(
            self.root, self.run["hashes"]["manifest"], self.run["run_id"]
        )
        records, required, summaries = evaluate_quality(
            self.manifest, self.policy, rows, self.records
        )
        from qtb.coordinator.calibration import cost_panels

        if "scope" in self.run:
            required = sorted(
                set(required) | {f"{self.prefix}5/{name}" for name in cost_panels(self)}
            )
        decision = make_decision(self.run, records, required, summaries)
        self.run["status"] = "complete"
        self.save()
        write_json(self.directory / "evidence.json", self.records)
        write_report(self.directory, decision)
        return decision

    def execute(self, smoke=False):
        cases = [c for c in self.manifest["cases"] if c["role"] not in {"timing", "memory"}]
        self.progress(
            f"{self.run['profile']}: {len(cases)} quality cases, "
            f"{sum(c['seeds_per_block'] for c in cases)} compiles per revision before checks."
        )
        try:
            self.build(need_control=not smoke)
            self.roundtrip(cases)
            if not smoke:
                from qtb.coordinator.calibration import preflight
                from qtb.coordinator.checks import behavior_checks, clifford_checks
                from qtb.coordinator.upstream import upstream_checks

                behavior_checks(self, revisions=("baseline",))
                baseline_bad = any(
                    r["subject"] == "reference" and r["result"] == "failed" for r in self.records
                )
                self.evidence(
                    record(
                        "baseline/preflight",
                        "correctness",
                        "failed" if baseline_bad else "passed",
                        "reference",
                    )
                )
                if baseline_bad:
                    return self.finish()
                try:
                    preflight(self, cases)
                except Incomplete as exc:
                    self.evidence(
                        record("calibration/cost", "completeness", "unresolved", detail=str(exc))
                    )
                behavior_checks(self, revisions=("evolved",))
                clifford_checks(self, cases)
                upstream_checks(self)
                if any(r["result"] == "failed" for r in self.records):
                    return self.finish()
            rows = self.quality(cases, smoke=smoke)
            if smoke:
                success = len(rows) == len(cases) * 2 and all(
                    r.get("checks") and all(c["status"] == "verified" for c in r["checks"])
                    for r in rows
                )
                result = {
                    "format": "qtb-smoke/1",
                    "success": success,
                    "run_id": self.run["run_id"],
                    "observations": len(rows),
                }
                write_json(self.directory / "smoke.json", result)
                return result
            self.audit(cases, rows)
            self.aggregate_checks(cases, rows)
            records, _, _ = evaluate_quality(self.manifest, self.policy, rows, self.records)
            if any(
                r["kind"] == "improvement" and r["result"] == "passed" for r in records
            ) and not any(r["result"] == "failed" for r in records):
                from qtb.coordinator.calibration import measure_costs

                try:
                    measure_costs(self)
                except Incomplete as exc:
                    self.evidence(
                        record(f"{self.prefix}5/timing", "cost", "unresolved", detail=str(exc))
                    )
            # Qualification is deliberately explicit; local unit-test success alone
            # cannot assert an inspected controlled-runner acceptance claim.
            qualification_file = (
                self.root / "qualifications" / (digest(self.run["hashes"]) + ".json")
            )
            qualified = False
            if qualification_file.exists():
                qualification = read_json(qualification_file)
                qualified = (
                    qualification.get("hashes") == self.run["hashes"]
                    and qualification.get("known_outcomes_passed") is True
                    and bool(qualification.get("reviewer"))
                    and bool(qualification.get("controlled_run"))
                    and qualification.get("machine") == self.run["machine"]
                )
                self.run["qualification"] = qualification
            self.evidence(
                record(
                    "harness/qualification",
                    "harness",
                    "passed" if qualified else "unresolved",
                    detail=self.policy["qualification"]["reason"],
                )
            )
            return self.finish()
        except (HarnessError, OSError, ValueError, subprocess.SubprocessError) as exc:
            self.evidence(record("harness/error", "harness", "failed", detail=str(exc)))
            if smoke:
                result = {"format": "qtb-smoke/1", "success": False, "error": str(exc)}
                write_json(self.directory / "smoke.json", result)
                return result
            return self.finish()


def evaluate_run(directory):
    directory = Path(directory).resolve()
    run, manifest, policy = (
        read_json(directory / name) for name in ("run.json", "manifest.json", "policy.json")
    )
    if digest(manifest) != run["hashes"]["manifest"] or digest(policy) != run["hashes"]["policy"]:
        raise HarnessError("Archived manifest or policy hash changed")
    rows = read_records(directory / "observations.jsonl")
    evidence = (
        read_json(directory / "evidence.json") if (directory / "evidence.json").exists() else []
    )
    records, required, summaries = evaluate_quality(manifest, policy, rows, evidence)
    decision = make_decision(run, records, required, summaries)
    if (directory / "decision.json").exists():
        decision["review"] = read_json(directory / "decision.json").get("review")
    write_report(directory, decision)
    return decision
