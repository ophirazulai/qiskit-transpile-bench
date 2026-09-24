"""Baseline-owned Python tests, seed-reshuffle derivation, and inline Rust tests."""

import shutil
import subprocess
from pathlib import Path

from qtb.canonical import digest, read_json, write_json
from qtb.coordinator.storage import read_records
from qtb.envbuild import run_logged, sanitized_environment
from qtb.errors import HarnessError
from qtb.evaluator import record

SEED_OFFSET = 1_000_003
RUNNER = """import functools
import inspect
import json
import sys
from pathlib import Path

root, output, offset = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
sys.path.insert(0, str(root))
import pytest
if offset:
    from qiskit.transpiler.passes import SabreLayout, SabreSwap
    for cls in (SabreLayout, SabreSwap):
        original = cls.__init__
        signature = inspect.signature(original)
        def wrapped(self, *args, _original=original, _signature=signature, **kwargs):
            bound = _signature.bind(self, *args, **kwargs)
            seed = bound.arguments.get("seed")
            if seed is not None:
                bound.arguments["seed"] = (int(seed) + offset) % (2**32)
            return _original(*bound.args, **bound.kwargs)
        cls.__init__ = functools.wraps(original)(wrapped)

class Recorder:
    def pytest_runtest_logreport(self, report):
        row = dict(nodeid=report.nodeid, when=report.when, outcome=report.outcome,
                   detail=str(report.longrepr) if report.failed else "")
        with output.open("a") as stream:
            stream.write(json.dumps(row) + "\\n")

raise SystemExit(pytest.main(sys.argv[4:], plugins=[Recorder()]))
"""


def prepare_tests(snapshot, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    source = Path(snapshot)
    if not (directory / "test").exists():
        shutil.copytree(source / "test", directory / "test")
        for name in ("pytest.ini", "setup.cfg", ".stestr.conf"):
            if (source / name).exists():
                shutil.copy2(source / name, directory / name)
    runner = directory / "run_tests.py"
    runner.write_text(RUNNER)
    return runner


def python_suite(build, snapshot, directory, locks, exclusions=(), offset=0):
    directory = Path(directory)
    runner = prepare_tests(snapshot, directory)
    results, log = directory / "tests.jsonl", directory / "tests.log"
    if results.exists():
        results.unlink()
    env = sanitized_environment()
    try:
        run_logged(
            [build["python"], "-m", "pip", "install", "-r", Path(locks) / "dev-tests.lock"],
            directory,
            env,
            log,
        )
        with log.open("ab") as stream:
            proc = subprocess.run(
                [
                    build["python"],
                    "-P",
                    runner,
                    directory,
                    results,
                    str(offset),
                    "test/python/transpiler",
                    "test/python/compiler",
                    "-q",
                    *[f"--deselect={node}" for node in exclusions],
                ],
                cwd=directory,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=1800,
                check=False,
            )
        status = (
            "passed" if proc.returncode == 0 else "failed" if proc.returncode == 1 else "unresolved"
        )
    except (HarnessError, subprocess.TimeoutExpired):
        status = "unresolved"
    rows = read_records(results)
    return {
        "result": status,
        "records": str(results),
        "log": str(log),
        "failed": sorted({r["nodeid"] for r in rows if r["outcome"] == "failed"}),
        "passed": sorted(
            {r["nodeid"] for r in rows if r["when"] == "call" and r["outcome"] == "passed"}
        ),
        "seed_offset": offset,
    }


def derive_exclusions(run_directory, locks):
    """Propose node IDs from a neutral seed offset; never approve them automatically."""
    directory = Path(run_directory).resolve()
    run = read_json(directory / "run.json")
    build = run["builds"]["baseline"]
    root = directory / "exclusion-derivation"
    ordinary = python_suite(build, build["snapshot"]["path"], root / "ordinary", locks)
    shuffled = python_suite(
        build, build["snapshot"]["path"], root / "reshuffled", locks, offset=SEED_OFFSET
    )
    proposal = {
        "format": "qtb-exclusions/1",
        "status": "unreviewed",
        "baseline_build": build["id"],
        "runner_hash": digest(RUNNER),
        "seed_offset": SEED_OFFSET,
        "ordinary": ordinary,
        "reshuffled": shuffled,
        "output_pinned_tests": sorted(set(shuffled["failed"]) & set(ordinary["passed"])),
        "preexisting_failures": ordinary["failed"],
        "review_instruction": "Inspect every proposed failure for an exact heuristic-output "
        "assertion; remove semantic failures before freezing the list.",
    }
    write_json(root / "exclusions.proposed.json", proposal)
    return proposal


def rust_suite(build, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "rust.log"
    env = sanitized_environment(
        {
            "RUSTUP_TOOLCHAIN": build["toolchain_channel"],
            "CARGO_HOME": str(Path(build["environment"]).parent / "cargo"),
        }
    )
    try:
        run_logged(
            ["cargo", "test", "--locked", "-p", "qiskit-transpiler"],
            Path(build["environment"]).parent / "source",
            env,
            log,
            timeout=1800,
        )
        return "passed"
    except HarnessError:
        # Cargo exit 101 also means compilation failed; only executed test
        # assertions establish a correctness failure.
        text = log.read_text() if log.exists() else ""
        return "failed" if "test result: FAILED." in text else "unresolved"


def upstream_checks(comparison):
    exclusions = read_json(
        comparison.data / "profiles" / comparison.run["profile"] / "exclusions.json"
    )
    changed = comparison.run.get("changed_paths", [])
    write_json(
        comparison.directory / "changed-tests.json",
        {
            "python_test_files": [p for p in changed if p.startswith("test/")],
            "possible_inline_test_regions": [p for p in changed if p.endswith(".rs")],
            "output_pinned_exclusions": exclusions,
        },
    )
    baseline = comparison.run["builds"]["baseline"]
    if exclusions.get("status") != "reviewed" or exclusions.get("baseline_build") != baseline["id"]:
        comparison.evidence(
            record(
                f"{comparison.prefix}1/upstream",
                "correctness",
                "unresolved",
                detail="Output-pinned exclusions require a reviewed derivation for this baseline "
                "build; run derive-exclusions on the archived run",
            )
        )
        return
    results = {}
    for revision, subject in (("baseline", "reference"), ("evolved", "evolved")):
        build = comparison.run["builds"][revision]
        result = python_suite(
            build,
            baseline["snapshot"]["path"],
            comparison.directory / f"upstream-{revision}",
            comparison.data / "envs",
            exclusions["output_pinned_tests"],
        )
        results[revision] = result
        status = result["result"]
        if revision == "evolved" and status == "failed":
            new = set(result["failed"]) - set(results["baseline"]["failed"])
            status = "failed" if new else "unresolved"
        comparison.evidence(
            record(
                f"{comparison.prefix}1/upstream/{revision}",
                "correctness",
                status,
                subject,
                **{k: v for k, v in result.items() if k != "result"},
            )
        )
    rust = {
        rev: rust_suite(comparison.run["builds"][rev], comparison.directory / f"upstream-{rev}")
        for rev in ("baseline", "evolved")
    }
    for revision, subject in (("baseline", "reference"), ("evolved", "evolved")):
        status = rust[revision]
        if revision == "evolved" and status == "failed" and rust["baseline"] != "passed":
            status = "unresolved"
        comparison.evidence(
            record(f"{comparison.prefix}1/upstream/rust/{revision}", "correctness", status, subject)
        )
    # The candidate's own Python suite is explicitly report-only.
    own = python_suite(
        comparison.run["builds"]["evolved"],
        comparison.run["builds"]["evolved"]["snapshot"]["path"],
        comparison.directory / "upstream-evolved-own",
        comparison.data / "envs",
    )
    write_json(comparison.directory / "upstream-evolved-own.json", own)
    all_passed = all(
        r["result"] == "passed"
        for r in comparison.records
        if r["id"].startswith(f"{comparison.prefix}1/upstream/")
    )
    comparison.evidence(
        record(
            f"{comparison.prefix}1/upstream",
            "correctness",
            "passed" if all_passed else "unresolved",
        )
    )
