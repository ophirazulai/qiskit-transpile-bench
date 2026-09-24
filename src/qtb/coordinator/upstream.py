"""Baseline-owned Python tests and inline Rust tests."""

import shutil
import subprocess
from pathlib import Path

from qtb.canonical import write_json
from qtb.coordinator.storage import read_records
from qtb.envbuild import run_logged, sanitized_environment
from qtb.errors import HarnessError
from qtb.evaluator import record

RUNNER = """import json
import sys
from pathlib import Path

root, output = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root))
import pytest

class Recorder:
    def pytest_runtest_logreport(self, report):
        row = dict(nodeid=report.nodeid, when=report.when, outcome=report.outcome,
                   detail=str(report.longrepr) if report.failed else "")
        with output.open("a") as stream:
            stream.write(json.dumps(row) + "\\n")

raise SystemExit(pytest.main(sys.argv[3:], plugins=[Recorder()]))
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


def python_suite(build, snapshot, directory, locks, timeout_s):
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
                    "test/python/transpiler",
                    "test/python/compiler",
                    "-q",
                ],
                cwd=directory,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
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
    }


def rust_suite(build, directory, timeout_s):
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
            timeout=timeout_s,
        )
        return "passed"
    except HarnessError:
        # Cargo exit 101 also means compilation failed; only executed test
        # assertions establish a correctness failure.
        text = log.read_text() if log.exists() else ""
        return "failed" if "test result: FAILED." in text else "unresolved"


def upstream_checks(comparison):
    budgets = comparison.policy["upstream_test_budgets_s"]
    changed = comparison.run.get("changed_paths", [])
    write_json(
        comparison.directory / "changed-tests.json",
        {
            "python_test_files": [p for p in changed if p.startswith("test/")],
            "possible_inline_test_regions": [p for p in changed if p.endswith(".rs")],
        },
    )
    baseline = comparison.run["builds"]["baseline"]
    results = {}
    for revision, subject in (("baseline", "reference"), ("evolved", "evolved")):
        build = comparison.run["builds"][revision]
        result = python_suite(
            build,
            baseline["snapshot"]["path"],
            comparison.directory / f"upstream-{revision}",
            comparison.data / "envs",
            budgets["python"],
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
        rev: rust_suite(
            comparison.run["builds"][rev],
            comparison.directory / f"upstream-{rev}",
            budgets["rust"],
        )
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
        budgets["python"],
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
