"""Baseline-owned Python tests and inline Rust tests.

Only the confirm profile runs these. The baseline's own failures are known-bad: the evolved
revision is judged only on tests that stop passing relative to the baseline.
"""

import configparser
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from qtb.canonical import write_json
from qtb.coordinator.storage import read_records
from qtb.envbuild import run_logged, sanitized_environment
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


def python_suite(build, snapshot, directory, locks, timeout_s):
    directory = Path(directory)
    runner = prepare_tests(snapshot, directory)
    results, log = directory / "tests.jsonl", directory / "tests.log"
    if results.exists():
        results.unlink()
    env = sanitized_environment()
    completed = False
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
    if log.exists():
        log.unlink()
    env = sanitized_environment(
        {
            "RUSTUP_TOOLCHAIN": build["toolchain_channel"],
            "CARGO_HOME": str(Path(build["environment"]).parent / "cargo"),
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
    python = {
        revision: python_suite(
            comparison.run["builds"][revision],
            baseline["snapshot"]["path"],
            comparison.directory / f"upstream-{revision}",
            comparison.data / "envs",
            budgets["python"],
        )
        for revision in ("baseline", "evolved")
    }
    rust = {
        revision: rust_suite(
            comparison.run["builds"][revision],
            comparison.directory / f"upstream-{revision}",
            budgets["rust"],
        )
        for revision in ("baseline", "evolved")
    }
    ids = []
    for suite, results in (("", python), ("rust/", rust)):
        base, evolved, regressions = judge(results["baseline"], results["evolved"])
        for revision, subject, (status, detail) in (
            ("baseline", "reference", base),
            ("evolved", "evolved", evolved),
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
        comparison.run["builds"]["evolved"],
        comparison.run["builds"]["evolved"]["snapshot"]["path"],
        comparison.directory / "upstream-evolved-own",
        comparison.data / "envs",
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
