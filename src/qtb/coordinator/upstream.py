"""Baseline-owned Python tests and evolved inline Rust tests."""

import shutil
import subprocess
from pathlib import Path

from qtb.canonical import write_json
from qtb.envbuild import run_logged, sanitized_environment
from qtb.errors import HarnessError
from qtb.evaluator import record


def upstream_checks(comparison):
    exclusions = __import__("qtb.canonical", fromlist=["read_json"]).read_json(
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
    if exclusions.get("status") != "reviewed":
        comparison.evidence(
            record(
                f"{comparison.prefix}1/upstream",
                "correctness",
                "unresolved",
                detail="Output-pinned exclusions require a reviewed reshuffled-baseline derivation",
            )
        )
        return
    baseline = Path(comparison.run["builds"]["baseline"]["snapshot"]["path"])
    test_root = comparison.directory / "baseline-tests"
    if not test_root.exists():
        test_root.mkdir()
        shutil.copytree(baseline / "test", test_root / "test")
        for name in ("pytest.ini", "setup.cfg", ".stestr.conf"):
            if (baseline / name).exists():
                shutil.copy2(baseline / name, test_root / name)
    runner = test_root / "run_tests.py"
    runner.write_text(
        "import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).parent))\n"
        "import pytest\nraise SystemExit(pytest.main(sys.argv[1:]))\n"
    )
    for revision, subject in (("baseline", "reference"), ("evolved", "evolved")):
        build = comparison.run["builds"][revision]
        python = build["python"]
        log = comparison.directory / f"upstream-{revision}.log"
        env = sanitized_environment()
        try:
            run_logged(
                [python, "-m", "pip", "install", "-r", comparison.data / "envs/dev-tests.lock"],
                test_root,
                env,
                log,
            )
            deselect = [f"--deselect={node}" for node in exclusions["output_pinned_tests"]]
            # Baseline tests run against both builds; only test/ is placed on sys.path.
            proc = subprocess.run(
                [
                    python,
                    "-P",
                    runner,
                    "test/python/transpiler",
                    "test/python/compiler",
                    "-q",
                    *deselect,
                ],
                cwd=test_root,
                env=env,
                stdout=log.open("ab"),
                stderr=subprocess.STDOUT,
                timeout=1800,
                check=False,
            )
            status = (
                "passed"
                if proc.returncode == 0
                else "failed"
                if proc.returncode == 1
                else "unresolved"
            )
        except (HarnessError, subprocess.TimeoutExpired):
            status = "unresolved"
        comparison.evidence(
            record(f"{comparison.prefix}1/upstream/{revision}", "correctness", status, subject)
        )
    evolved = comparison.run["builds"]["evolved"]
    env = sanitized_environment(
        {
            "RUSTUP_TOOLCHAIN": comparison.run["builds"]["baseline"]["identity"]["toolchain"]
            .splitlines()[0]
            .split()[1]
        }
    )
    # Use the exact toolchain channel persisted by build_revision.
    env["RUSTUP_TOOLCHAIN"] = evolved["toolchain_channel"]
    env["CARGO_HOME"] = str(Path(evolved["environment"]).parent / "cargo")
    try:
        run_logged(
            ["cargo", "test", "--locked", "-p", "qiskit-transpiler"],
            Path(evolved["environment"]).parent / "source",
            env,
            comparison.directory / "upstream-rust.log",
            timeout=1800,
        )
        rust_status = "passed"
    except HarnessError:
        rust_status = "unresolved"
    comparison.evidence(record(f"{comparison.prefix}1/upstream/rust", "correctness", rust_status))
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
