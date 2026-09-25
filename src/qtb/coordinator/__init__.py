"""Comparison orchestration; this process never imports Qiskit."""

import hashlib
import os
import random
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from qtb.canonical import (
    circuit_lines,
    digest,
    file_hash,
    read_circuit,
    read_json,
    verify_artifact,
    write_json,
)
from qtb.config import (
    STAGES,
    case_hash,
    coordinator_identity,
    data_root,
    implementation_identity,
    load_profile,
)
from qtb.coordinator.process import run_worker
from qtb.coordinator.runlog import RunLog, depth, duration, step
from qtb.coordinator.storage import (
    append_record,
    cache_quality_observation,
    cached_quality_observation,
    locked,
    prune_outputs,
    prune_prefix_outputs,
    quality_cache_key,
    read_records,
    register_decision,
    runner_lock,
)
from qtb.envbuild import (
    baseline_toolchain,
    build_revision,
    diff_snapshots,
    machine_identity,
    run_logged,
    sanitized_environment,
    snapshot,
    verify_build,
)
from qtb.errors import HarnessError, Incomplete
from qtb.evaluator import evaluate_quality, record
from qtb.evaluator.scope import changed_scope, covered
from qtb.metrics import StructuralChecker, layout_errors
from qtb.metrics.replay import replay
from qtb.reporter import make_decision, write_report


def structural_result(
    path, target, layout, input_width, initial_layout=None, constraint_form="target"
):
    hasher = hashlib.sha256()
    lines = circuit_lines(path, hasher)
    header = next(lines)
    checker = StructuralChecker(header, target, constraint_form)
    for operation in lines:
        checker.consume(operation)
    errors = layout_errors(layout, input_width, header["num_qubits"], initial_layout)
    checker.errors.extend(errors)
    result = dict(checker.result(), output_hash=hasher.hexdigest())
    if not errors:
        initial = layout["initial_index_layout"] if layout else list(range(input_width))
        final = layout["final_index_layout"] if layout else list(range(input_width))
        result["union_width"] = len(
            checker.active_qubits | set(initial[:input_width]) | set(final[:input_width])
        )
    return result


# Quality batches compiled at once. Quality metrics are deterministic and nothing reads the
# compile times of these jobs; timing and memory are measured separately, exclusively.
QUALITY_BATCHES_IN_FLIGHT = 9


def routing_replay_seeds(case, seeds):
    """C6 scope: scored/basis panels in full, static case guards on ten B0 seeds."""
    if case["role"] == "scored" or (
        case["role"] == "guard" and case.get("panel") in {"basis-cx", "basis-ecr"}
    ):
        return list(seeds)
    if case["role"] == "guard" and not case.get("panel"):
        return [seed for seed in seeds if seed < 10]
    return []


def cost_stage_due(builds, records):
    """Whether to measure cost panels: ``"improved"``, ``"aa"`` or ``None``.

    Costs are measured once the candidate improves with nothing failed. Identical builds
    (an A/A run) never improve, but their cost panels are the known-outcome check that
    timing and memory measurement report no change, so they are measured unless a
    correctness or guard check failed.
    """
    improved = any(r["kind"] == "improvement" and r["result"] == "passed" for r in records)
    if improved and not any(r["result"] == "failed" for r in records):
        return "improved"
    same_build = (
        "baseline" in builds
        and "evolved" in builds
        and builds["baseline"]["id"] == builds["evolved"]["id"]
    )
    blocking = any(r["result"] == "failed" and r["kind"] != "improvement" for r in records)
    if same_build and not blocking:
        return "aa"
    return None


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
        self.data = data_root()
        self.fixtures = self.data / "fixtures"
        self.manifest, self.policy, hashes = load_profile(profile, self.data, verify=False)
        self.prefix = "CA" if profile == "confirm-profile" else "IA"
        self.records = []
        self.declaration = change_scope
        if resume:
            self.directory = Path(resume).resolve()
            self.run = read_json(self.directory / "run.json")
            if self.run["hashes"].get("coordinator") != coordinator_identity():
                raise HarnessError(
                    "Resume with the archived harness version; coordinator code changed"
                )
            implementation = implementation_identity()
            if self.run["hashes"].get("implementation", implementation) != implementation:
                raise HarnessError("Resume with the archived worker and verifier implementation")
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
                "coverage_gaps": self.manifest.get("coverage_gaps", []),
                "sources": {
                    "baseline": str(Path(baseline).resolve()),
                    "evolved": str(Path(evolved or baseline).resolve()),
                },
            }
            self.run["hashes"]["coordinator"] = coordinator_identity()
            write_json(self.directory / "manifest.json", self.manifest)
            write_json(self.directory / "policy.json", self.policy)
            self.save()
        # Timestamped progress and per-step durations, next to report.md.
        self.progress = RunLog(self.directory / "progress.log", progress)

    def save(self):
        write_json(self.directory / "run.json", self.run)

    def evidence(self, row):
        # Repeated phases replace an earlier unresolved aggregate, never duplicate IDs.
        self.records = [r for r in self.records if r["id"] != row["id"]] + [row]
        write_json(self.directory / "evidence.json", self.records)

    def build(self, need_evolved=True):
        with locked(runner_lock(), shared=True):
            return self._build(need_evolved)

    def _build(self, need_evolved=True):
        load_profile(self.run["profile"], self.data, verify=True)
        build_root = self.directory / "builds"
        build_root.mkdir(exist_ok=True)
        snapshots = {}
        with step(self, "Snapshot sources"):
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
            with step(self, "Build harness wheel"):
                project = Path(__file__).resolve().parents[3]
                if (project / "pyproject.toml").exists():
                    command = ["uv", "build", "--wheel", "--out-dir", str(wheels), str(project)]
                    run_logged(
                        command,
                        self.directory,
                        sanitized_environment(),
                        self.directory / "harness-build.log",
                    )
                else:
                    from qtb.coordinator.wheel import repack_installed_harness

                    repack_installed_harness(wheels)
                found = list(wheels.glob("*.whl"))
        if len(found) != 1:
            raise HarnessError("Expected exactly one harness wheel")
        self.run["hashes"]["harness"] = file_hash(found[0])
        self.run["hashes"]["implementation"] = implementation_identity()
        toolchain = baseline_toolchain(snapshots["baseline"])
        revisions = ["baseline"] + (["evolved"] if need_evolved else [])
        pending = []
        for revision in revisions:
            directory = build_root / f"{revision}-build"
            if (directory / "build.json").exists():
                build = read_json(directory / "build.json")
                verify_build(build)
                if build["snapshot"]["tree_hash"] != snapshots[revision]["tree_hash"]:
                    raise HarnessError("Saved build does not match the source snapshot")
                self.run["builds"][revision] = build
                self.save()
                self.progress(f"Reusing the saved {revision} build.")
                continue
            if directory.exists():
                directory.rename(
                    directory.with_name(directory.name + ".failed-" + uuid.uuid4().hex[:8])
                )
            pending.append(revision)

        def compile_revision(revision, level):
            with step(self, f"{revision} Qiskit build", level):
                self.progress(
                    f"Preparing {revision} environment with baseline Rust toolchain {toolchain}."
                )
                return build_revision(
                    snapshots[revision],
                    build_root / f"{revision}-build",
                    self.data / "envs",
                    found[0],
                    toolchain,
                    cache_root=self.root / "build-cache",
                    cache_slot=revision,
                    progress=self.progress,
                )

        # Revisions compile concurrently: the final LTO step of one build leaves
        # most cores idle. Each has its own directory, CARGO_HOME and target/.
        # A build that finished is recorded even if the other one fails.
        with step(self, f"Qiskit builds ({', '.join(pending) or 'none pending'})"):
            level = depth(self)
            with ThreadPoolExecutor(max_workers=max(1, len(pending))) as pool:
                futures = {
                    revision: pool.submit(compile_revision, revision, level)
                    for revision in pending
                }
        errors = [futures[r].exception() for r in pending if futures[r].exception()]
        for revision in pending:
            if futures[revision].exception() is None:
                self.run["builds"][revision] = futures[revision].result()
        self.save()
        if errors:
            raise errors[0]
        with step(self, "Verifier environment"):
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
        with locked(runner_lock(), shared=True):
            result = run_worker(self.run["builds"][revision], job, directory, hash_seed)
        for row in result:
            row.setdefault("job_file", str(directory / "job.json"))
        return result

    def roundtrip(self, cases, revisions=("baseline", "evolved")):
        seen, specs, keys = set(), [], []
        for revision in revisions:
            for case in cases:
                key = revision, case["circuit"]["sha256"], case["target"]["sha256"]
                if key in seen:
                    continue
                seen.add(key)
                specs.append((revision, case, "roundtrip", [0]))
                keys.append(key)
        self.progress(f"Round-tripping {len(specs)} distinct inputs, {self.workers} at a time.")
        for (revision, case, *_), key, rows in zip(specs, keys, self.jobs(specs), strict=True):
            row = rows[0]
            if (
                row["status"] != "ok"
                or row.get("circuit_hash") != key[1]
                or row.get("target_hash") != key[2]
            ):
                raise HarnessError(
                    f"Input roundtrip failed on {revision}: {case['case_id']}: {row}"
                )
        self.evidence(record("harness/roundtrip", "harness", "passed"))

    def roundtrip_cases(self):
        cases = list(self.manifest["cases"])
        cases.extend(read_json(self.fixtures / "correctness-suite.json")["cases"])
        cases.extend(
            dict(c, circuit=c["clifford_variant"])
            for c in self.manifest["cases"]
            if "clifford_variant" in c
        )
        return cases

    @property
    def workers(self):
        """Concurrency for correctness jobs; never used for timing or memory measurement."""
        return max(1, min(12, (os.cpu_count() or 2) - 1))

    def jobs(self, specs):
        """Run independent ``job`` specs concurrently; results keep the order of ``specs``."""
        specs = list(specs)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(lambda spec: self.job(*spec), specs))

    def oracle(self, case, result, oracle, reference=None, **extra):
        return self.verify_many([(case, result, oracle, reference, extra)])[0]

    def verifier_identity(self):
        return digest(
            {
                "implementation": self.run["hashes"].get("implementation")
                or implementation_identity(),
                "locks": {
                    name: file_hash(self.data / "envs" / name)
                    for name in ("common.lock", "verifier.lock")
                },
            }
        )

    def verify_many(self, requests):
        """Verify (case, result, oracle, reference, extra) requests.

        The verifier is a pure function of its job, so results are keyed by content: the
        output, reference and target file hashes plus the oracle options. A seed or revision
        that reproduces an output already verified, in this run or an earlier one, reuses that
        result. Only decisive results (verified or mismatch) are cached.
        """
        if not requests:
            return []
        identity = self.verifier_identity()
        jobs, keys = [], []
        for case, result, oracle, reference, extra in requests:
            ref = reference or (
                case["semantic_reference"]
                if case["semantic_reference"]["kind"] == "frozen_circuit"
                else case["circuit"]
            )
            reference_path = verify_artifact(self.fixtures, ref, circuit=True)
            job = {
                "protocol": "qtb-verifier/1",
                "reference": str(reference_path),
                "reference_hash": ref["sha256"],
                "output": result["output"],
                "layout": result["layout"],
                "input_domain": case["input_domain"],
                "oracle": oracle,
                **(extra or {}),
            }
            content = {
                k: file_hash(v) if k in {"output", "reference", "target"} else v
                for k, v in job.items()
            }
            jobs.append(job)
            keys.append(digest({"verifier": identity, "job": content}))
        cache = self.root / "verifier-cache"
        results = {}
        for key in set(keys):
            path = cache / key[:2] / f"{key}.json"
            if path.exists():
                results[key] = dict(read_json(path), cached=True)
        pending = {}
        for key, job in zip(keys, jobs, strict=True):
            if key not in results:
                pending.setdefault(key, job)
        if pending:
            fresh = self.run_verifier(pending)
            for key, result in fresh.items():
                if result.get("status") in {"verified", "mismatch"}:
                    write_json(cache / key[:2] / f"{key}.json", result)
            results.update(fresh)
        return [dict(results[key]) for key in keys]

    def run_verifier(self, pending):
        """Run unique verifier jobs in batches across a process pool."""
        from qtb.coordinator.process import run_verifier_batch

        entries = []
        for key, job in pending.items():
            directory = self.directory / "oracle-jobs" / uuid.uuid4().hex
            directory.mkdir(parents=True)
            write_json(directory / "job.json", job)
            entries.append((key, directory, job["oracle"]))
        size = max(1, min(50, -(-len(entries) // self.workers)))
        chunks = [entries[i : i + size] for i in range(0, len(entries), size)]

        def verify_chunk(chunk):
            results = {}
            while chunk:
                batch = self.directory / "oracle-jobs" / f"batch-{uuid.uuid4().hex}"
                pairs = [
                    {"job": str(d / "job.json"), "out": str(d / "result.json")}
                    for _, d, _ in chunk
                ]
                problem = run_verifier_batch(self.run["verifier_python"], pairs, batch)
                missing = []
                for key, directory, oracle in chunk:
                    if (directory / "result.json").exists():
                        results[key] = read_json(directory / "result.json")
                    else:
                        missing.append((key, directory, oracle))
                if not missing:
                    break
                # Jobs run in order, so the first missing job is the one that stopped the
                # process. It is unverified; the rest get a fresh process.
                key, _, oracle = missing[0]
                results[key] = {"status": "unverified", "oracle": oracle, "detail": problem}
                chunk = missing[1:]
            return results

        merged = {}
        with locked(runner_lock(), shared=True):
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for results in pool.map(verify_chunk, chunks):
                    merged.update(results)
        return merged

    def quality(self, cases, smoke=False, revisions=("baseline", "evolved")):
        block = "B0"
        builds = self.run["builds"]
        independent_aa = (
            "baseline" in builds
            and "evolved" in builds
            and builds["baseline"]["id"] == builds["evolved"]["id"]
        )
        use_cache = not smoke and not independent_aa
        saved = read_records(self.directory / "observations.jsonl")
        completed = {(r["case_id"], r["revision"], r["seed"], r["seed_block"]) for r in saved}
        batch_size = self.policy["measurement_protocol"]["quality_batch_size"]
        tasks = []
        for revision in revisions:
            for case in cases:
                target = read_json(self.fixtures / case["target"]["file"])
                count = 1 if smoke else case["seeds_per_block"]
                seeds = [
                    s
                    for s in range(count)
                    if (case["case_id"], revision, s, block) not in completed
                ]
                key = quality_cache_key(
                    self.run["builds"][revision],
                    case,
                    self.run["machine"],
                    self.policy["measurement_protocol"],
                    self.run["hashes"]["implementation"],
                )
                cache = self.root / "quality-cache" / key
                if use_cache:
                    remaining = []
                    for seed in seeds:
                        path = cache / f"{seed}.json"
                        if path.exists():
                            cached = cached_quality_observation(path)
                            if cached is not None:
                                cached.update(
                                    revision=revision,
                                    cached=True,
                                    case_id=case["case_id"],
                                    case_hash=case_hash(case),
                                )
                                cached["id"] = digest(
                                    {
                                        k: cached[k]
                                        for k in (
                                            "case_id",
                                            "case_hash",
                                            "build_id",
                                            "revision",
                                            "seed",
                                            "seed_block",
                                        )
                                    }
                                )
                                append_record(self.directory / "observations.jsonl", cached)
                                continue
                        remaining.append(seed)
                    seeds = remaining
                for start in range(0, len(seeds), batch_size):
                    batch = seeds[start : start + batch_size]
                    tasks.append((revision, case, target, batch, cache))
        workers = max(1, min(QUALITY_BATCHES_IN_FLIGHT, self.workers))
        self.progress(
            f"Quality: {len(tasks)} batches of up to {batch_size} seeds, {workers} at a time."
        )
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = [
                pool.submit(self.quality_batch, revision, case, target, batch, smoke)
                for revision, case, target, batch, _ in tasks
            ]
            # Batches compile concurrently but are committed in plan order, so
            # observations.jsonl and evidence.json do not depend on scheduling.
            for number, (task, future) in enumerate(zip(tasks, futures, strict=True), 1):
                revision, case, _, batch, cache = task
                finished, lite, timings = future.result()
                started = time.monotonic()
                verdicts = self.verify_many(
                    [(case, result, "C1-lite", None, {}) for _, result, _ in lite]
                )
                for (observation, _, union_width), check in zip(lite, verdicts, strict=True):
                    observation["checks"].append(
                        dict(check, oracle="C1-lite", union_width=union_width)
                    )
                timings["verify"] = time.monotonic() - started
                for observation, result in finished:
                    if result["status"] != "ok":
                        subject = "reference" if revision == "baseline" else "evolved"
                        self.evidence(
                            record(
                                f"failure/{observation['id']}",
                                "completeness",
                                "unresolved" if result.get("not_attempted") else "failed",
                                subject,
                                detail=result.get("error", "Worker failed"),
                            )
                        )
                    append_record(self.directory / "observations.jsonl", observation)
                    if result["status"] == "ok" and use_cache:
                        cache_quality_observation(cache, result["seed"], observation)
                failed = sum(result["status"] != "ok" for _, result in finished)
                self.progress(
                    f"Quality batch {number}/{len(tasks)}: {revision} {case['case_id']} "
                    f"seeds {batch[0]}-{batch[-1]}: "
                    + ", ".join(f"{name} {duration(value)}" for name, value in timings.items())
                    + (f"; {failed} failed" if failed else "")
                )
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        return read_records(self.directory / "observations.jsonl")

    def quality_batch(self, revision, case, target, batch, smoke=False):
        """Compile and check one seed batch; runs on a pool thread, so it records nothing.

        Returns the (observation, worker result) pairs, the C1-lite requests still to
        verify, and how long each part took.
        """
        timings = {}
        started = time.monotonic()
        rows = self.job(revision, case, "quality", batch)
        timings["compile"] = time.monotonic() - started
        started = time.monotonic()
        prefixes = (
            self.routing_batch(revision, case, [r["seed"] for r in rows if r["status"] == "ok"])
            if not smoke
            else {}
        )
        timings["routing prefixes"] = time.monotonic() - started
        started = time.monotonic()
        finished, lite = [], []
        for result in rows:
            observation = {
                "format": "qtb-observation/1",
                "case_id": case["case_id"],
                "case_hash": case_hash(case),
                "revision": revision,
                "build_id": self.run["builds"][revision]["id"],
                "seed": result["seed"],
                "seed_block": "B0",
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
                    for k in ("case_id", "case_hash", "build_id", "revision", "seed", "seed_block")
                }
            )
            if result["status"] == "ok":
                structural = structural_result(
                    result["output"],
                    target,
                    result["layout"],
                    case["logical_qubits"],
                    case["options"].get("initial_layout"),
                    case["constraint_form"],
                )
                if structural["output_hash"] != result["output_hash"]:
                    raise HarnessError("Exported output hash mismatch")
                observation.update({k: structural[k] for k in ("D2", "N2") if k in structural})
                observation.update(
                    output=result["output"],
                    output_hash=result["output_hash"],
                    layout=result["layout"],
                    fingerprint=result.get("fingerprint", {}),
                    free_parameters=result.get("free_parameters", []),
                )
                observation["checks"].append(dict(structural, oracle="C0"))
                if not smoke and structural["status"] == "verified":
                    self.check_routing(revision, case, observation, prefixes)
                    if (
                        self.run["profile"] == "confirm-profile"
                        and case["role"] == "scored"
                        and case["logical_qubits"] <= 25
                        and result["seed"] < 10
                        and not observation.get("free_parameters")
                    ):
                        union_width = structural["union_width"]
                        if union_width <= 25:
                            lite.append((observation, result, union_width))
                        else:
                            observation["checks"].append(
                                {
                                    "status": "unverified",
                                    "detail": "Union width exceeds limit",
                                    "oracle": "C1-lite",
                                    "union_width": union_width,
                                }
                            )
            finished.append((observation, result))
        timings["checks"] = time.monotonic() - started
        return finished, lite, timings

    def routing_batch(self, revision, case, seeds):
        seeds = routing_replay_seeds(case, seeds)
        if not seeds:
            return {}
        initial = self.job(revision, case, "prefix", seeds, [f"drop_stage:{s}" for s in STAGES[1:]])
        routed = self.job(revision, case, "prefix", seeds, [f"drop_stage:{s}" for s in STAGES[3:]])
        left, right = ({r["seed"]: r for r in rows} for rows in (initial, routed))
        missing = {"status": "error"}
        return {seed: (left.get(seed, missing), right.get(seed, missing)) for seed in seeds}

    def check_routing(self, revision, case, observation, prefixes):
        seed = observation["seed"]
        if not routing_replay_seeds(case, [seed]):
            return
        initial, routed = prefixes[seed]
        if initial["status"] != "ok" or routed["status"] != "ok":
            observation["checks"].append(
                {"oracle": "C6", "status": "unverified", "detail": "Prefix compilation failed"}
            )
            return
        h0, ops0 = read_circuit(initial["output"])
        h1, ops1 = read_circuit(routed["output"])
        elided = initial["layout"]["final_index_layout"] if initial["layout"] else None
        checked = replay(ops0, ops1, routed["layout"], h0["num_qubits"], h1["num_qubits"], elided)
        if observation["layout"] == routed["layout"]:
            checked["layout_equality"] = "matched"
        elif case["optimization_level"] == 3:
            checked.update(
                layout_equality="skipped",
                layout_disagreement={
                    "reason": "Level-3 optimization may reapply layout",
                    "full": observation["layout"],
                    "routing_prefix": routed["layout"],
                },
            )
        else:
            checked.update(
                status="mismatch",
                detail="Full and routing-prefix layouts differ",
                layout_equality="mismatch",
            )
        checked.update(
            oracle="C6",
            reference_hash=observation["reference_hash"],
            input_domain=case["input_domain"],
            prefix_jobs=[initial["job_file"], routed["job_file"]],
            prefix_outputs=[
                {
                    "stage": stage,
                    "output": result["output"],
                    "output_hash": result.get("output_hash"),
                    "job_file": result["job_file"],
                }
                for stage, result in (("initial", initial), ("routed", routed))
            ],
        )
        observation["checks"].append(checked)

    def audit(self, cases, observations, revisions=("baseline", "evolved")):
        by_case = {c["case_id"]: c for c in cases}
        rng = random.Random(self.policy["rng_seed"])
        ok = True
        specs, sampled = [], []
        for revision in revisions:
            rows = [
                r
                for r in observations
                if r["revision"] == revision and r.get("output_hash") and r["seed_block"] == "B0"
            ]
            count = min(len(rows), max(10, int(len(rows) * 0.05 + 0.999)))
            if not rows:
                ok = False
            for i, row in enumerate(rng.sample(rows, count)):
                specs.append(
                    (
                        revision,
                        by_case[row["case_id"]],
                        "quality",
                        [row["seed"]],
                        (),
                        "1" if i % 2 else "0",
                    )
                )
                sampled.append(row)
        self.progress(f"Determinism audit: recompiling {len(specs)} sampled seeds.")
        for row, results in zip(sampled, self.jobs(specs), strict=True):
            result = results[0]
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
        if self.run["profile"] == "confirm-profile":
            eligible = {}
            for row in quality:
                for check in row["checks"]:
                    if check["oracle"] != "C1-lite":
                        continue
                    key = row["case_id"], row["seed"]
                    eligible.setdefault(key, {})[row["revision"]] = check
                    if check["status"] == "mismatch":
                        self.evidence(
                            record(
                                f"C1-lite/{row['id']}",
                                "correctness",
                                "failed",
                                "reference" if row["revision"] == "baseline" else "evolved",
                            )
                        )
            applicable = [
                pair
                for pair in eligible.values()
                if pair.get("baseline", {}).get("union_width", 26) <= 25
            ]
            okay = bool(applicable) and all(
                pair.get(rev, {}).get("status") == "verified"
                for pair in applicable
                for rev in ("baseline", "evolved")
            )
            self.evidence(
                record(
                    "CA1/C1-lite",
                    "correctness",
                    "passed" if okay else "unresolved",
                    eligible_observations=len(applicable),
                )
            )
        expected = sum(c["seeds_per_block"] for c in cases) * 2
        self.evidence(
            record(
                f"{self.prefix}6/completeness",
                "completeness",
                "passed" if len(quality) == expected else "unresolved",
            )
        )
        for oracle in ("C0", "C6"):
            required = (
                expected
                if oracle == "C0"
                else 2 * sum(
                    len(routing_replay_seeds(case, range(case["seeds_per_block"])))
                    for case in cases
                )
            )
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
                if len(checks) == required and all(c["status"] == "verified" for c in checks)
                else "unresolved"
            )
            self.evidence(record(f"{self.prefix}1/{oracle}", "correctness", status))
        by_case = {c["case_id"]: c for c in cases}
        clifford = read_records(self.directory / "clifford.jsonl")
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
            checks.extend(
                c
                for c in clifford
                if c.get("case_id") == case["case_id"] and c.get("revision") == "evolved"
            )
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
        with step(self, "Evaluate and write report"):
            decision = self._finish()
        self.summarize()
        return decision

    def summarize(self):
        """Append the table of step durations to progress.log."""
        summary = getattr(self.progress, "summary", None)
        if summary:
            summary()

    def _finish(self):
        from qtb.coordinator.costs import replay_costs, required_cost_panels

        self.records = replay_costs(
            self.directory, self.run, self.manifest, self.policy, self.records
        )
        rows = read_records(self.directory / "observations.jsonl")
        self.run["decisions_before"] = register_decision(
            self.root, self.run["hashes"]["manifest"], self.run["run_id"]
        )
        records, required, summaries = evaluate_quality(
            self.manifest, self.policy, rows, self.records
        )
        if "scope" in self.run:
            required = sorted(
                set(required) | {f"{self.prefix}5/{name}" for name in required_cost_panels(self)}
            )
        decision = make_decision(
            self.run, records, required, summaries, rows, self.manifest, self.policy
        )
        self.run["status"] = "complete"
        self.save()
        write_json(self.directory / "evidence.json", self.records)
        write_report(self.directory, decision)
        if decision["status"] in {"PASS", "NO_IMPROVEMENT"}:
            limit = self.policy["measurement_protocol"]["output_retention_bytes"]
            prune_outputs(self.directory, rows, limit)
            prune_prefix_outputs(self.directory, rows, limit)
        return decision

    def execute(self, smoke=False):
        cases = [c for c in self.manifest["cases"] if c["role"] not in {"timing", "memory"}]
        self.progress(
            f"{self.run['profile']}: {len(cases)} quality cases, "
            f"{sum(c['seeds_per_block'] for c in cases)} compiles per revision before checks."
        )
        try:
            with step(self, "Build"):
                self.build()
            with step(self, "Input roundtrip"):
                self.roundtrip(cases if smoke else self.roundtrip_cases())
            if not smoke:
                from qtb.coordinator.checks import behavior_checks, clifford_checks
                from qtb.coordinator.upstream import upstream_checks

                with step(self, "C1-C5 suite (baseline)"):
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
                with step(self, "C1-C5 suite (evolved)"):
                    behavior_checks(self, revisions=("evolved",))
                with step(self, "C7 Clifford variants"):
                    clifford_checks(self, cases)
                # Qiskit's own test suite gates acceptance, not every iteration.
                if self.run["profile"] == "confirm-profile":
                    with step(self, "Upstream Qiskit tests"):
                        upstream_checks(self)
                if any(r["result"] == "failed" for r in self.records):
                    return self.finish()
            with step(self, "Quality (C0 + C6 routing replay)"):
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
                self.summarize()
                return result
            with step(self, "Determinism audit"):
                self.audit(cases, rows)
            with step(self, "Aggregate checks"):
                self.aggregate_checks(cases, rows)
            records, _, _ = evaluate_quality(self.manifest, self.policy, rows, self.records)
            due = cost_stage_due(self.run["builds"], records)
            if due:
                from qtb.coordinator.costs import measure_costs

                if due == "aa":
                    self.progress("Identical builds: measuring cost panels as an A/A check.")
                with step(self, "Cost panels (timing/memory)"):
                    try:
                        measure_costs(self)
                    except Incomplete as exc:
                        self.evidence(
                            record(
                                f"{self.prefix}5/timing", "cost", "unresolved", detail=str(exc)
                            )
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
                self.summarize()
                return result
            return self.finish()
