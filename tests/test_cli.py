import pytest

from qtb.cli import main, parser


def test_compile_requires_both_source_trees():
    args = parser().parse_args(
        ["compile", "--baseline", "baseline", "--evolved", "evolved", "--store", "store"]
    )
    assert args.command == "compile"
    assert args.baseline.name == "baseline"
    assert args.evolved.name == "evolved"
    assert args.store.name == "store"
    assert args.results_root.name == "results"

    with pytest.raises(SystemExit) as exc:
        parser().parse_args(["compile", "--baseline", "baseline"])
    assert exc.value.code == 64


def test_compile_accepts_confirm_profile():
    args = parser().parse_args(
        [
            "compile",
            "--baseline",
            "baseline",
            "--evolved",
            "evolved",
            "--profile",
            "confirm-profile",
        ]
    )
    assert args.profile == "confirm-profile"


@pytest.mark.parametrize(
    "command", ["quality", "correctness", "unit-tests", "cost", "decide", "clean"]
)
def test_stage_commands_take_only_the_results_root(command):
    assert parser().parse_args([command, "--results-root", "s"]).results_root.name == "s"
    for option in ("--workers", "--rerun", "--force", "--store", "--run"):
        with pytest.raises(SystemExit) as exc:
            parser().parse_args([command, option, "x"])
        assert exc.value.code == 64


@pytest.mark.parametrize(
    "command",
    ["compare", "smoke", "evaluate", "calibrate", "derive-exclusions", "report", "repro", "review"],
)
def test_removed_commands_are_rejected(command):
    with pytest.raises(SystemExit) as exc:
        parser().parse_args([command])
    assert exc.value.code == 64


@pytest.mark.parametrize("option", ["--change-scope", "--resume", "--workers"])
def test_removed_compile_options_are_rejected(option):
    with pytest.raises(SystemExit) as exc:
        parser().parse_args(
            ["compile", "--baseline", "b", "--evolved", "e", "--store", "s", option, "x"]
        )
    assert exc.value.code == 64


def test_compile_needs_an_existing_store(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("QTB_STORE", raising=False)
    session = tmp_path / "session"
    arguments = ["compile", "--baseline", "b", "--evolved", "e", "--results-root", str(session)]
    assert main(arguments) == 64
    assert "QTB_STORE" in capsys.readouterr().err
    assert main([*arguments, "--store", str(tmp_path / "typo")]) == 64
    assert not (tmp_path / "typo").exists()
    assert not session.exists()


def test_stages_on_a_missing_session_are_precondition_failures(tmp_path):
    for command in ("quality", "correctness", "unit-tests", "cost", "decide", "clean"):
        assert main([command, "--results-root", str(tmp_path / "nothing")]) == 41
