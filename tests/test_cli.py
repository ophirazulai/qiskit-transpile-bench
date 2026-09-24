import pytest

from qtb.cli import parser


@pytest.mark.parametrize("command", ["smoke", "compare"])
def test_supported_commands_require_both_source_trees(command):
    args = parser().parse_args(
        [command, "--baseline", "baseline", "--evolved", "evolved"]
    )
    assert args.command == command
    assert args.baseline.name == "baseline"
    assert args.evolved.name == "evolved"

    with pytest.raises(SystemExit) as exc:
        parser().parse_args([command, "--baseline", "baseline"])
    assert exc.value.code == 64


def test_compare_accepts_confirm_profile():
    args = parser().parse_args(
        [
            "compare",
            "--baseline",
            "baseline",
            "--evolved",
            "evolved",
            "--profile",
            "confirm-profile",
        ]
    )
    assert args.profile == "confirm-profile"


def test_evaluate_accepts_archived_run():
    assert parser().parse_args(["evaluate", "--run", "archive/run"]).run.name == "run"


@pytest.mark.parametrize(
    "command",
    ["calibrate", "derive-exclusions", "report", "repro", "review"],
)
def test_removed_commands_are_rejected(command):
    with pytest.raises(SystemExit) as exc:
        parser().parse_args([command])
    assert exc.value.code == 64
