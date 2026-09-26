"""The Python launcher: named arguments, validation before bsub, the manager submission."""

import subprocess
import sys
from pathlib import Path

import pytest

from qtb.canonical import read_json

from lsf import lsf_directory, submit
from lsf.retry import Ledger
from lsf.tests.conftest import FakeScheduler

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def paths(tmp_path):
    for name in ("store", "sessions", "baseline", "evolved"):
        (tmp_path / name).mkdir()
    return tmp_path


def argv(paths, *extra, session="sessions/s1"):
    return [
        "--store",
        str(paths / "store"),
        "--results-root",
        str(paths / session),
        "--baseline",
        str(paths / "baseline"),
        "--evolved",
        str(paths / "evolved"),
        "--cost-ncpus",
        "56",
        *extra,
    ]


def resolve(arguments, environ=None):
    return submit.resolve(submit.parser().parse_args(arguments), environ or {})


def test_explicit_arguments_win_over_environment_fallbacks(paths):
    other = paths / "other-store"
    other.mkdir()
    environ = {
        "QTB_STORE": str(other),
        "QTB_SESSION": str(paths / "sessions/from-env"),
        "LSF_LOG_LEVEL": "debug",
    }
    config = resolve(argv(paths), environ)
    assert config["store"] == str((paths / "store").resolve())
    assert config["session"] == str((paths / "sessions/s1").resolve())
    assert config["log_level"] == "DEBUG"
    config = resolve(
        [
            "--baseline",
            str(paths / "baseline"),
            "--evolved",
            str(paths / "evolved"),
            "--cost-ncpus",
            "56",
        ],
        environ,
    )
    assert config["store"] == str(other.resolve())
    assert config["session"].endswith("sessions/from-env")
    assert resolve(argv(paths, "--log-level", "warning"), environ)["log_level"] == "WARNING"
    assert resolve(argv(paths))["log_level"] == "INFO"


def test_the_configuration_is_fixed_where_the_plan_fixes_it(paths):
    config = resolve(argv(paths, "--queue", "normal", "--cost-queue", "quiet"))
    resources = config["resources"]
    assert {job: r["slots"] for job, r in resources.items()} == {
        "compile": 16,
        "quality": 16,
        "correctness": 16,
        "unit-tests": 16,
        "cost": 9,
        "clean": 16,
        "manager": 16,
    }
    assert resources["cost"]["queue"] == "quiet" and resources["compile"]["queue"] == "normal"
    assert config["tier"] == {"model": None, "ncpus": 56}
    assert config["cost_selector"]["max_r1m"] == 10.0
    assert config["max_cost_retries"] == 20 and config["unit_tests"] is True
    help_text = submit.parser().format_help()
    for group in (
        "session inputs",
        "shared paths",
        "execution",
        "cluster resources",
        "quiet-node selection",
        "logging",
    ):
        assert group in help_text
    for absent in ("--dry-run", "--resume", "--name", "--max-retries", "--cost-slots"):
        assert absent not in help_text


@pytest.mark.parametrize(
    "extra, session, message",
    [
        ((), "sessions/exists", "already exists"),
        ((), "missing-parent/s1", "does not exist"),
        (("--cost-ncpus", "8"), "sessions/s1", "must exceed"),
        (("--cost-model", "bad model"), "sessions/s1", "invalid --cost-model"),
        (("--cost-wall", "tomorrow"), "sessions/s1", "--cost-wall"),
        (("--compile-mem-gb", "0"), "sessions/s1", "must be positive"),
        (("--manager-wall", "8:00"), "sessions/s1", "--manager-wall must be longer"),
        (("--queue", "no;way"), "sessions/s1", "invalid queue"),
    ],
)
def test_invalid_configurations_are_refused_before_submission(paths, extra, session, message):
    (paths / "sessions/exists").mkdir()
    with pytest.raises(submit.Refused, match=message):
        resolve(argv(paths, *extra, session=session))


def test_a_tier_and_a_store_are_required(paths):
    arguments = argv(paths)
    at = arguments.index("--cost-ncpus")
    with pytest.raises(submit.Refused, match="hardware tier"):
        resolve(arguments[:at] + arguments[at + 2 :])
    with pytest.raises(submit.Refused, match="--store"):
        resolve(arguments[2:])
    with pytest.raises(submit.Refused, match="is not an existing directory"):
        resolve(argv(paths, "--store", str(paths / "nope")))


def test_submission_records_intent_and_prints_where_to_look(paths, capsys):
    backend = FakeScheduler()
    assert submit.main(argv(paths, "--log-level", "DEBUG"), backend=backend, environ={}) == 0
    session = (paths / "sessions/s1").resolve()
    assert not session.exists()  # compile creates it
    lsf_dir = lsf_directory(session)
    launch = read_json(lsf_dir / "launch.json")
    assert launch["session"] == str(session) and launch["log_level"] == "DEBUG"
    (spec,) = backend.submitted
    command = spec.argv()
    assert command[:2] == ["bsub", "-J"] and "-r" in command
    assert command[command.index("-n") + 1] == "16"
    assert command[-6:] == [sys.executable, "-P", "-m", "lsf.manager", "--lsf-dir", str(lsf_dir)]
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert ledger.job("manager")["status"] == "submitted" and ledger.job("manager")["job_id"]
    out = capsys.readouterr().out
    assert "LSF accepted it; the benchmark has not run yet" in out
    for shown in ("session:", "logs:", "manager log:", "ledger:", "report:", "manager-1.log"):
        assert shown in out
    # A second launch at the same path is refused: there is no implicit resume.
    assert submit.main(argv(paths), backend=backend, environ={}) == 41


def test_submission_failures_and_missing_commands_are_reported(paths, capsys):
    backend = FakeScheduler()
    backend.script["manager"] = ["rejected"]
    assert submit.main(argv(paths), backend=backend, environ={}) == 40
    assert "did not accept the manager (rejected)" in capsys.readouterr().err
    backend = FakeScheduler()
    backend.missing_commands = lambda: ["bsub"]
    assert submit.main(argv(paths, session="sessions/s2"), backend=backend, environ={}) == 41
    assert not lsf_directory(paths / "sessions/s2").exists()


def test_the_launcher_runs_as_a_file_without_shadowing_the_standard_library():
    result = subprocess.run(
        [sys.executable, str(ROOT / "lsf/submit.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT / "lsf",
    )
    assert result.returncode == 0, result.stderr
    assert "quiet-node selection" in result.stdout


def test_launcher_holds_the_orchestration_lock_until_acceptance_is_saved(paths):
    from qtb.coordinator.storage import LockBusy, locked

    backend = FakeScheduler()
    original = backend.submit

    def immediate_start(spec):
        lsf_dir = lsf_directory(paths / "sessions/s1")
        with pytest.raises(LockBusy), locked(lsf_dir / "orchestration.lock", wait=False):
            pytest.fail("manager could read a ledger the launcher will overwrite")
        return original(spec)

    backend.submit = immediate_start
    assert submit.main(argv(paths), backend=backend, environ={}) == 0
