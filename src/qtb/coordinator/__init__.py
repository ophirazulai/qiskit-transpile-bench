"""Comparison orchestration; this process never imports Qiskit.

A ``Comparison`` is one stage's view of a session directory. ``compile`` creates the session
with ``Comparison.create``; every later stage opens it with ``Comparison.open``. Evidence is
written per stage, under ``stages/<stage>/``; ``qtb.coordinator.stages`` runs the stages.
"""

import hashlib
import os
import random
import shutil
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from qtb.canonical import (
    atomic_bytes,
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
    cached_quality_observation,
    invalidate_quality_entry,
    locked,
    quality_cache_key,
    quality_entry_valid,
    read_records,
    runner_lock,
    store_quality_observation,
)
from qtb.coordinator.store import Store, build_key
from qtb.envbuild import (
    baseline_toolchain,
    build_identity,
    build_into,
    diff_snapshots,
    machine_identity,
    run_logged,
    sanitized_environment,
    snapshot,
    verify_build,
)
from qtb.errors import HarnessError, Precondition, Usage
from qtb.evaluator import record
from qtb.evaluator.scope import changed_scope, covered
from qtb.metrics import StructuralChecker, layout_errors
from qtb.metrics.replay import replay


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


def same_build(builds):
    return (
        "baseline" in builds
        and "evolved" in builds
        and builds["baseline"]["id"] == builds["evolved"]["id"]
    )


def gate(builds, records, required_quality=(), required_improvements=()):
    """Whether correctness, unit tests and cost run: ``(state, reason)``.

    ``records`` are the evaluated quality records together with the compile and quality
    evidence. Every non-improvement record must pass, including the round-trip, the audit,
    C0, C6, C1-lite, the guards and completeness. Unlike ``cost_stage_due``, an unresolved
    check closes the gate, also on an A/A run.

    - ``improved``: every improvement record passed as well;
    - ``aa``: identical builds, so no improvement is possible; the later stages still run as
      a check of the harness;
    - ``closed``: anything else. The session is decided from the quality evidence alone.
    """
    by_id = {r["id"]: r for r in records}
    improvement_ids = set(required_improvements) or {
        r["id"] for r in records if r["kind"] == "improvement"
    }
    blocking = sorted(
        r["id"]
        for r in records
        if r["kind"] != "improvement" and r["result"] != "passed"
    )
    blocking += sorted(
        f"{id_} (missing)"
        for id_ in set(required_quality) - improvement_ids - by_id.keys()
    )
    if "harness/roundtrip" not in by_id and "harness/roundtrip" not in set(required_quality):
        blocking.insert(0, "harness/roundtrip (missing)")
    if blocking:
        more = f" and {len(blocking) - 5} more" if len(blocking) > 5 else ""
        shown = ", ".join(blocking[:5]) + more
        return "closed", f"quality checks did not pass: {shown}"
    if same_build(builds):
        return "aa", "identical builds (A/A): later stages run as a harness check"
    if improvement_ids and all(
        by_id.get(id_, {}).get("result") == "passed" for id_ in improvement_ids
    ) and all(r["result"] == "passed" for r in records if r["kind"] == "improvement"):
        return "improved", "quality improved with every quality check passing"
    unresolved = sorted(
        id_ for id_ in improvement_ids if by_id.get(id_, {}).get("result") != "failed"
    )
    if unresolved:
        return "closed", "improvement unresolved: " + ", ".join(unresolved)
    return "closed", "no improvement"


def stage_coverage(run, cases, observations, clifford, prefix):
    """``*1/stage-coverage``: every scored case has verified coverage of the changed stages.

    It combines the quality checks (C0, C6, C1-lite) with the C7 Clifford checks, so
    ``decide`` computes it once both the quality and correctness stages are complete.
    """
    quality = [r for r in observations if r["seed_block"] == "B0"]
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
        scope = run["scope"][str(case["optimization_level"])]
        if not covered(case, checks, scope):
            coverage.append(case["case_id"])
    return record(
        f"{prefix}1/stage-coverage",
        "correctness",
        "unresolved" if coverage else "passed",
        cases=coverage,
    )


def worker_count():
    """Concurrency for correctness jobs; never used for timing or memory measurement.

    On LSF, the slots granted to the job (``LSB_DJOB_NUMPROC``); otherwise one less than the
    CPU count, at most 12.
    """
    granted = os.environ.get("LSB_DJOB_NUMPROC", "")
    if granted.isdigit() and int(granted) > 0:
        return int(granted)
    return max(1, min(12, (os.cpu_count() or 2) - 1))


def quality_cases(manifest):
    return [c for c in manifest["cases"] if c["role"] not in {"timing", "memory"}]


def profile_prefix(profile):
    return "CA" if profile == "confirm-profile" else "IA"


def harness_wheel(directory):
    found = sorted((Path(directory) / "harness-wheel").glob("*.whl"))
    return found[0] if found else None


class Comparison:
    """One stage's view of a session directory (see the module docstring)."""

    def _setup(self, results_root, stage, profile):
        self.directory = Path(results_root).resolve()
        self.stage = stage
        self.data = data_root()
        self.fixtures = self.data / "fixtures"
        self.manifest, self.policy, self.hashes = load_profile(profile, self.data, verify=False)
        self.prefix = profile_prefix(profile)
        self.records = []
        # The host this stage runs on: quality cache keys and cost bundles use it.
        self.machine = machine_identity()

    @classmethod
    def create(cls, baseline, evolved, profile, results_root, store, progress=print):
        """``compile``: create the session's ``run.json``, or resume an unfinished one.

        The caller holds the session locks. A session with other sources, another store or
        another profile is refused.
        """
        self = cls.__new__(cls)
        self._setup(results_root, "compile", profile)
        sources = {
            "baseline": str(Path(baseline).resolve()),
            "evolved": str(Path(evolved).resolve()),
        }
        store = str(Path(store).resolve())
        if (self.directory / "run.json").exists():
            self.run = read_json(self.directory / "run.json")
            wanted = {"sources": sources, "store": store, "profile": profile}
            found = {key: self.run.get(key) for key in wanted}
            if found != wanted:
                changed = ", ".join(k for k in wanted if wanted[k] != found[k])
                raise Usage(
                    f"{self.directory} is a session for other inputs ({changed} differ); "
                    "start a new session with another --results-root"
                )
            self._check_harness()
        else:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.run = {
                "format": "qtb-run/2",
                "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
                + "-"
                + uuid.uuid4().hex[:8],
                "profile": profile,
                "hashes": dict(self.hashes, coordinator=coordinator_identity()),
                "created_at": datetime.now(UTC).isoformat(),
                "session": str(self.directory),
                "store": store,
                "machine": self.machine,
                "builds": {},
                "status": "created",
                "coverage_gaps": self.manifest.get("coverage_gaps", []),
                "sources": sources,
            }
            write_json(self.directory / "manifest.json", self.manifest)
            write_json(self.directory / "policy.json", self.policy)
            self.save()
        self._open_stage(progress)
        return self

    @classmethod
    def open(cls, results_root, stage, progress=print):
        """Every stage after ``compile``: load the session and check the harness is unchanged."""
        directory = Path(results_root).resolve()
        if not (directory / "run.json").exists():
            raise Precondition(f"{directory} is not a session; run compile first")
        run = read_json(directory / "run.json")
        self = cls.__new__(cls)
        self._setup(directory, stage, run["profile"])
        self.run = run
        self._check_harness()
        self._open_stage(progress)
        return self

    def _check_harness(self):
        """A stage runs with the harness that compiled its session, as ``--resume`` did."""
        wheel = harness_wheel(self.directory)
        hint = f"; install the archived harness wheel {wheel}" if wheel else ""
        if self.run["hashes"].get("coordinator") != coordinator_identity():
            raise Precondition(f"Harness code changed since compile{hint}")
        implementation = self.run["hashes"].get("implementation")
        if implementation is not None and implementation != implementation_identity():
            raise Precondition(f"Worker or verifier code changed since compile{hint}")
        if self.run["hashes"]["manifest"] != self.hashes["manifest"]:
            raise Precondition(f"The profile's manifest changed since compile{hint}")
        if self.run["hashes"]["policy"] != self.hashes["policy"]:
            raise Precondition(f"The profile's policy changed since compile{hint}")

    def _open_stage(self, progress):
        self.stage_dir = self.directory / "stages" / self.stage
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        path = self.stage_dir / "evidence.json"
        # A retry starts without the crash record of the attempt it resumes.
        self.records = [
            r
            for r in (read_json(path) if path.exists() else [])
            if r["id"] != f"harness/error/{self.stage}"
        ]
        self.store = Store(self.run["store"]) if self.run.get("store") else None
        self.progress = RunLog(self.stage_dir / "progress.log", progress)

    def save(self):
        """``run.json`` has one writer: the compile stage."""
        if getattr(self, "stage", "compile") != "compile":
            raise HarnessError("Only the compile stage writes run.json")
        write_json(self.directory / "run.json", self.run)

    @property
    def evidence_path(self):
        return self.directory / "stages" / self.stage / "evidence.json"

    def evidence(self, row):
        # Repeated phases replace an earlier unresolved aggregate, never duplicate IDs.
        self.records = [r for r in self.records if r["id"] != row["id"]] + [row]
        write_json(self.evidence_path, self.records)

    def reset_evidence(self):
        self.records = []
        write_json(self.evidence_path, self.records)

    def committed(self, *stages):
        """Evidence of other stages that ended ``complete``; never partial evidence."""
        from qtb.coordinator.stages import committed_evidence

        return [row for stage in stages for row in committed_evidence(self.directory, stage)]

    def build(self):
        with locked(runner_lock(), shared=True):
            return self._build()

    def _build(self):
        load_profile(self.run["profile"], self.data, verify=True)
        build_root = self.directory / "builds"
        build_root.mkdir(exist_ok=True)
        snapshots = {}
        with step(self, "Snapshot sources"):
            for revision, source in self.run["sources"].items():
                path = build_root / f"{revision}.snapshot.json"
                if path.exists():
                    snapshots[revision] = read_json(path)
                else:
                    destination = build_root / revision
                    if destination.exists():
                        # A killed snapshot can leave a partial tree before its manifest
                        # is committed. It cannot be used as a source for a build.
                        shutil.rmtree(destination)
                    snapshots[revision] = snapshot(source, destination)
        self.run["changed_paths"] = diff_snapshots(snapshots["baseline"], snapshots["evolved"])
        self.run["scope"] = {
            str(level): changed_scope(self.run["changed_paths"], level) for level in range(4)
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
        envs = self.data / "envs"
        with step(self, "Resolve build identities"):
            identities = {
                revision: build_identity(snapshots[revision], envs, toolchain)
                for revision in ("baseline", "evolved")
            }
        pending = []
        directory = build_root / "evolved-build"
        if (directory / "build.json").exists():
            build = read_json(directory / "build.json")
            verify_build(build)
            if build["snapshot"]["tree_hash"] != snapshots["evolved"]["tree_hash"]:
                raise HarnessError("Saved build does not match the source snapshot")
            self.run["builds"]["evolved"] = build
            self.save()
            self.progress("Reusing the saved evolved build.")
        else:
            if directory.exists():
                directory.rename(
                    directory.with_name(directory.name + ".failed-" + uuid.uuid4().hex[:8])
                )
            pending.append("evolved")

        def evolved_build(level):
            with step(self, "evolved Qiskit build", level):
                self.progress(
                    f"Preparing evolved environment with baseline Rust toolchain {toolchain}."
                )
                return build_into(
                    snapshots["evolved"],
                    directory,
                    identities["evolved"],
                    envs,
                    found[0],
                    toolchain,
                    label="evolved",
                    progress=self.progress,
                )

        def baseline_build(level):
            return self.baseline_build(
                snapshots["baseline"], identities["baseline"], found[0], toolchain, level
            )

        # Both revisions are prepared concurrently: the final LTO step of one build leaves
        # most cores idle. Each has its own directory, CARGO_HOME and target/. A build that
        # finished is recorded even if the other one fails.
        tasks = {"baseline": baseline_build, **({"evolved": evolved_build} if pending else {})}
        with step(self, f"Qiskit builds (baseline from the store{', evolved' if pending else ''})"):
            level = depth(self)
            with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
                futures = {revision: pool.submit(task, level) for revision, task in tasks.items()}
        errors = [f.exception() for f in futures.values() if f.exception()]
        for revision, future in futures.items():
            if future.exception() is None:
                self.run["builds"][revision] = future.result()
        self.save()
        if errors:
            raise errors[0]
        with step(self, "Verifier environment"):
            self.build_verifier(found[0])
        self.run["status"] = "built"
        self.save()

    def baseline_build(self, snapshot_info, identity, wheel, toolchain, level=0):
        """The baseline build from the store, building it there on a miss.

        The key covers the build identity and the harness wheel installed in the venv. A
        directory without ``READY`` is an interrupted build; it is rebuilt under the key's
        lock at the same, final path, because a virtual environment cannot be moved.
        """
        key = build_key(identity, self.run["hashes"]["harness"])
        reused = True
        build = self.store.ready_build(key)
        if build is None:
            with locked(self.store.build_lock(key)):
                build = self.store.ready_build(key)
                if build is None:
                    reused = False
                    entry = self.store.build_dir(key)
                    if entry.exists():
                        self.progress(f"Repairing the interrupted baseline build {key[:12]}.")
                        shutil.rmtree(entry)
                    with step(self, "baseline Qiskit build (into the store)", level):
                        self.progress(
                            f"Building baseline {key[:12]} into the store with "
                            f"Rust toolchain {toolchain}."
                        )
                        build = build_into(
                            snapshot_info,
                            entry,
                            identity,
                            self.data / "envs",
                            wheel,
                            toolchain,
                            wheel_cache=self.store.wheels,
                            label="baseline",
                            progress=self.progress,
                        )
                    atomic_bytes(entry / "READY", (datetime.now(UTC).isoformat() + "\n").encode())
        if reused:
            self.progress(f"Reusing baseline build {key} from the store.")
        verify_build(build)
        return dict(build, store_key=key, reused=reused)

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
        return worker_count()

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
        that reproduces an output already verified in this session, by any stage, reuses that
        result. Only decisive results (verified or mismatch) are cached; writes are atomic.
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
        cache = self.directory / "verifier-cache"
        results = {}
        for key in set(keys):
            path = cache / key[:2] / f"{key}.json"
            if path.exists():
                try:
                    cached = read_json(path)
                except HarnessError:
                    continue
                if cached.get("status") in {"verified", "mismatch"}:
                    results[key] = dict(cached, cached=True)
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

    def baseline_quality_key(self, case):
        """Store key of the baseline quality rows for ``case`` on this stage's host."""
        return quality_cache_key(
            self.run["builds"]["baseline"],
            case,
            self.machine,
            self.policy["measurement_protocol"],
            self.run["hashes"]["implementation"],
        )

    def quality(self, cases, revisions=("baseline", "evolved")):
        """Compile every quality seed with its C0, C6 and C1-lite checks.

        Baseline rows come from the store when present; fresh baseline rows are added to it.
        Evolved rows are never stored. An A/A session bypasses the stored rows, so it stays
        an independent check. Seeds already in ``observations.jsonl`` are not redone.
        """
        block = "B0"
        use_store = self.store is not None and not same_build(self.run["builds"])
        saved = read_records(self.directory / "observations.jsonl")
        completed = {(r["case_id"], r["revision"], r["seed"], r["seed_block"]) for r in saved}
        batch_size = self.policy["measurement_protocol"]["quality_batch_size"]
        tasks = []
        for revision in revisions:
            for case in cases:
                target = read_json(self.fixtures / case["target"]["file"])
                seeds = [
                    s
                    for s in range(case["seeds_per_block"])
                    if (case["case_id"], revision, s, block) not in completed
                ]
                cache = key = None
                if revision == "baseline" and use_store:
                    key = self.baseline_quality_key(case)
                    cache = self.store.quality_dir(key)
                    if not quality_entry_valid(cache):
                        self.progress(
                            f"Stored baseline quality {key[:12]} was invalidated by a failed "
                            f"determinism audit; recomputing {case['case_id']}."
                        )
                        cache = None
                if cache is not None:
                    remaining = []
                    for seed in seeds:
                        path = cache / f"{seed}.json"
                        if path.exists():
                            cached = cached_quality_observation(path)
                            if cached is not None:
                                cached.update(
                                    revision=revision,
                                    cached=True,
                                    cached_from=key,
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
                pool.submit(self.quality_batch, revision, case, target, batch)
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
                    if result["status"] == "ok" and cache is not None:
                        store_quality_observation(cache, result["seed"], observation)
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

    def quality_batch(self, revision, case, target, batch):
        """Compile and check one seed batch; runs on a pool thread, so it records nothing.

        Returns the (observation, worker result) pairs, the C1-lite requests still to
        verify, and how long each part took.
        """
        timings = {}
        started = time.monotonic()
        rows = self.job(revision, case, "quality", batch)
        timings["compile"] = time.monotonic() - started
        started = time.monotonic()
        prefixes = self.routing_batch(
            revision, case, [r["seed"] for r in rows if r["status"] == "ok"]
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
                if structural["status"] == "verified":
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
        if not ok and self.store is not None and "baseline" in revisions:
            # Stored baseline rows cannot remain reusable after a failed determinism audit.
            # Correctness and unit-test entries stay: the audit did not test them.
            for case in cases:
                invalidate_quality_entry(
                    self.store.quality_dir(self.baseline_quality_key(case)),
                    f"Determinism audit failed in session {self.directory}",
                )

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
