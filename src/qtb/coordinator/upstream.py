"""Baseline-owned Python tests and inline Rust tests: the optional ``unit-tests`` stage.

The baseline's own failures are known-bad: the evolved revision is judged only on tests that
stop passing relative to the baseline. The baseline half is read from the store when a
complete result for the same build, harness, test tree, budgets and CPU model exists.

A stored build is immutable. The suites never install into its environment (the test
dependencies were installed at build time from ``dev-tests.lock``), never write bytecode
into it, and build Rust tests with a session-local ``CARGO_HOME`` and ``CARGO_TARGET_DIR``.
"""

import configparser
import gzip
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from qtb.canonical import atomic_bytes, digest, file_hash, read_json, write_json
from qtb.coordinator.storage import read_records
from qtb.envbuild import SERIAL, run_logged, sanitized_environment
from qtb.errors import HarnessError
from qtb.evaluator import record

# The guard matters: macOS starts multiprocessing workers with "spawn", which re-imports the
# main script. Without it every worker re-runs the whole suite and then exits, breaking the
# pool of each parallel-transpile test.
RUNNER = """import json
import sys
from pathlib import Path


class Recorder:
    def __init__(self, output):
        self.output = output

    def write(self, row):
        with self.output.open("a") as stream:
            stream.write(json.dumps(row) + "\\n")

    def pytest_runtest_logreport(self, report):
        self.write(dict(nodeid=report.nodeid, when=report.when, outcome=report.outcome,
                        detail=str(report.longrepr) if report.failed else ""))

    def pytest_collectreport(self, report):
        # A module that no longer imports must not silently drop its tests.
        if report.failed:
            self.write(dict(nodeid=report.nodeid, when="collect", outcome="failed",
                            detail=str(report.longrepr)))


def main():
    root, output = Path(sys.argv[1]), Path(sys.argv[2])
    sys.path.insert(0, str(root))
    import pytest

    return pytest.main(sys.argv[3:], plugins=[Recorder(output)])


if __name__ == "__main__":
    raise SystemExit(main())
"""

EMPTY_CONFIG = "[pytest]\n"
PYTEST_COMPLETED = {0, 1}  # 0: all passed, 1: some tests failed. Anything else: incomplete.
RUST_TEST = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\s*$", re.MULTILINE)


def pytest_config(source):
    """The snapshot's own pytest configuration, never the harness project's."""
    source = Path(source)
    if (source / "pytest.ini").exists():
        return (source / "pytest.ini").read_text()
    for name, section in (("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        if (source / name).exists():
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read(source / name)
            except configparser.Error:
                continue
            if parser.has_section(section):
                body = "".join(f"{k} = {v}\n" for k, v in parser.items(section))
                return "[pytest]\n" + body
    if (source / "pyproject.toml").exists():
        options = (
            tomllib.loads((source / "pyproject.toml").read_text())
            .get("tool", {})
            .get("pytest", {})
            .get("ini_options")
        )
        if options:
            lines = []
            for key, value in options.items():
                if isinstance(value, list):
                    value = "\n    " + "\n    ".join(map(str, value))
                lines.append(f"{key} = {value}\n")
            return "[pytest]\n" + "".join(lines)
    return EMPTY_CONFIG


def prepare_tests(snapshot, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    source = Path(snapshot)
    if not (directory / "test").exists():
        shutil.copytree(source / "test", directory / "test")
        for name in ("setup.cfg", ".stestr.conf"):
            if (source / name).exists():
                shutil.copy2(source / name, directory / name)
    # An explicit config file pins rootdir and node IDs to this directory, so the harness
    # project's own pytest settings never apply and node IDs match across revisions.
    (directory / "pytest.ini").write_text(pytest_config(source))
    runner = directory / "run_tests.py"
    runner.write_text(RUNNER)
    return runner


def compact_results(results):
    """Gzip ``tests.jsonl``, keeping failure text only for failed tests; returns the path."""
    results = Path(results)
    rows = read_records(results)
    lines = b"".join(
        json.dumps(
            row if row.get("outcome") == "failed" else dict(row, detail="")
        ).encode()
        + b"\n"
        for row in rows
    )
    packed = results.with_name(results.name + ".gz")
    atomic_bytes(packed, gzip.compress(lines, mtime=0))
    results.unlink(missing_ok=True)
    return packed


def python_suite(build, snapshot, directory, timeout_s):
    directory = Path(directory)
    runner = prepare_tests(snapshot, directory)
    results, log = directory / "tests.jsonl", directory / "tests.log"
    for stale in (results, results.with_name(results.name + ".gz")):
        stale.unlink(missing_ok=True)
    env = sanitized_environment({"PYTHONDONTWRITEBYTECODE": "1"})
    completed = False
    try:
        with log.open("ab") as stream:
            proc = subprocess.run(
                [
                    build["python"],
                    "-P",
                    runner,
                    directory,
                    results,
                    "-c",
                    directory / "pytest.ini",
                    f"--rootdir={directory}",
                    "-p",
                    "no:cacheprovider",
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
        completed = proc.returncode in PYTEST_COMPLETED
    except (HarnessError, subprocess.TimeoutExpired):
        pass
    rows = read_records(results)
    return {
        "completed": completed,
        "records": str(compact_results(results)),
        "log": str(log),
        "failed": sorted({r["nodeid"] for r in rows if r["outcome"] == "failed"}),
        "passed": sorted(
            {r["nodeid"] for r in rows if r["when"] == "call" and r["outcome"] == "passed"}
        ),
    }


def rust_suite(build, directory, timeout_s, cargo_home=None):
    """``cargo test`` in the build's source, with the test build under ``directory``.

    ``CARGO_TARGET_DIR`` is ``directory/target`` and is deleted afterwards. ``cargo_home``
    defaults to a fresh ``directory/cargo``, so a stored build's own ``cargo/`` is never used.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "rust.log"
    if log.exists():
        log.unlink()
    if cargo_home is None:
        cargo_home = directory / "cargo"
        cargo_home.mkdir(exist_ok=True)
        (cargo_home / "config.toml").write_text("[net]\nretry = 2\n")
    target = directory / "target"
    env = sanitized_environment(
        {
            "RUSTUP_TOOLCHAIN": build["toolchain_channel"],
            "CARGO_HOME": str(cargo_home),
            "CARGO_TARGET_DIR": str(target),
        }
    )
    try:
        # --no-fail-fast: every test binary runs, so failures compare test by test.
        run_logged(
            ["cargo", "test", "--locked", "--no-fail-fast", "-p", "qiskit-transpiler"],
            Path(build["environment"]).parent / "source",
            env,
            log,
            timeout=timeout_s,
        )
    except HarnessError:
        pass
    finally:
        # The debug test build is 2 GB and nothing reads it again.
        shutil.rmtree(target, ignore_errors=True)
    text = log.read_text(errors="replace") if log.exists() else ""
    outcomes = {name: outcome for name, outcome in RUST_TEST.findall(text)}
    # Cargo exit 101 also means compilation failed; only executed tests establish results.
    completed = "test result:" in text and "could not compile" not in text
    return {
        "completed": completed,
        "log": str(log),
        "failed": sorted(n for n, o in outcomes.items() if o == "FAILED"),
        "passed": sorted(n for n, o in outcomes.items() if o == "ok"),
    }


def judge(baseline, evolved):
    """Result for each revision; the baseline's failures are known-bad, never violations."""
    if not baseline["completed"]:
        base = ("unresolved", "Baseline suite did not complete")
    elif baseline["failed"]:
        base = ("unresolved", f"{len(baseline['failed'])} known-bad baseline failures")
    else:
        base = ("passed", "")
    if not evolved["completed"]:
        return base, ("unresolved", "Evolved suite did not complete"), []
    if not baseline["completed"]:
        if evolved["failed"]:
            return base, ("unresolved", "No complete baseline to compare failures with"), []
        return base, ("passed", ""), []
    # Anything the baseline passes must still pass: a failure, an error, a skip or a test
    # that disappeared (for example after a collection error) is a regression.
    regressions = sorted(
        (set(evolved["failed"]) - set(baseline["failed"]))
        | (set(baseline["passed"]) - set(evolved["passed"]))
    )
    if regressions:
        return base, ("failed", f"{len(regressions)} tests regressed against baseline"), regressions
    return base, ("passed", ""), []


def test_tree_hash(snapshot_info):
    """The baseline's own test files: the suite that runs against both revisions."""
    return digest(
        {path: entry for path, entry in snapshot_info["files"].items() if path.startswith("test/")}
    )


def baseline_unit_tests_key(comparison):
    """Store key of the baseline suites' results, or ``None`` when they must be rerun."""
    from qtb.coordinator import same_build

    builds = comparison.run["builds"]
    if comparison.store is None or same_build(builds):
        return None
    baseline = builds["baseline"]
    return digest(
        {
            "build": baseline["id"],
            "harness": comparison.run["hashes"]["harness"],
            "dev_tests_lock": file_hash(comparison.data / "envs" / "dev-tests.lock"),
            "test_tree": test_tree_hash(baseline["snapshot"]),
            "budgets": comparison.policy["upstream_test_budgets_s"],
            "machine_cpu": comparison.machine.get("cpu"),
            "worker_environment": SERIAL,
        }
    )


def _store_suites(comparison, key, python, rust):
    """Publish the baseline results with their logs, all paths inside the entry."""

    def fill(partial, final):
        for name, result, fields in (
            ("python", python, ("records", "log")),
            ("rust", rust, ("log",)),
        ):
            stored = dict(result)
            for field in fields:
                source = Path(result[field])
                if source.exists():
                    shutil.copy2(source, partial / f"{name}-{source.name}")
                stored[field] = str(final / f"{name}-{source.name}")
            write_json(partial / f"{name}.json", stored)

    comparison.store.publish_results(
        "unit-tests", key, fill, {"session": str(comparison.directory)}
    )


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
    builds = comparison.run["builds"]
    baseline = builds["baseline"]
    key = baseline_unit_tests_key(comparison)
    entry = comparison.store.read_results("unit-tests", key) if key else None
    python, rust = {}, {}
    if entry is not None:
        comparison.progress(f"Baseline unit tests: from the store (key {key[:12]}).")
        python["baseline"] = dict(read_json(entry / "python.json"), cached_from=key)
        rust["baseline"] = dict(read_json(entry / "rust.json"), cached_from=key)
    else:
        python["baseline"] = python_suite(
            baseline,
            baseline["snapshot"]["path"],
            comparison.directory / "upstream-baseline",
            budgets["python"],
        )
        rust["baseline"] = rust_suite(
            baseline, comparison.directory / "upstream-baseline", budgets["rust"]
        )
        if key and python["baseline"]["completed"] and rust["baseline"]["completed"]:
            _store_suites(comparison, key, python["baseline"], rust["baseline"])
    evolved = builds["evolved"]
    python["evolved"] = python_suite(
        evolved,
        baseline["snapshot"]["path"],
        comparison.directory / "upstream-evolved",
        budgets["python"],
    )
    # The evolved build lives in the session, so its own crates can be reused.
    rust["evolved"] = rust_suite(
        evolved,
        comparison.directory / "upstream-evolved",
        budgets["rust"],
        cargo_home=Path(evolved["environment"]).parent / "cargo",
    )
    ids = []
    for suite, results in (("", python), ("rust/", rust)):
        base, judged, regressions = judge(results["baseline"], results["evolved"])
        for revision, subject, (status, detail) in (
            ("baseline", "reference", base),
            ("evolved", "evolved", judged),
        ):
            ids.append(f"{comparison.prefix}1/upstream/{suite}{revision}")
            details = {k: v for k, v in results[revision].items() if k not in {"passed"}}
            details["passed_count"] = len(results[revision]["passed"])
            if revision == "baseline":
                details["known_bad"] = details.pop("failed")
            else:
                details["regressions"] = regressions
            comparison.evidence(
                record(ids[-1], "correctness", status, subject, detail=detail, **details)
            )
    # The candidate's own Python suite is explicitly report-only.
    own = python_suite(
        evolved,
        evolved["snapshot"]["path"],
        comparison.directory / "upstream-evolved-own",
        budgets["python"],
    )
    write_json(comparison.directory / "upstream-evolved-own.json", own)
    evolved_passed = all(
        r["result"] == "passed"
        for r in comparison.records
        if r["id"] in ids and r["subject"] == "evolved"
    )
    comparison.evidence(
        record(
            f"{comparison.prefix}1/upstream",
            "correctness",
            "passed" if evolved_passed else "unresolved",
        )
    )
