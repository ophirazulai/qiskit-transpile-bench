"""Re-pinning an unfinished cost stage to a later harness (``qtb.coordinator.amend``)."""

import pytest

from qtb.canonical import read_json, write_json
from qtb.coordinator.amend import amend_cost, effective, notes
from qtb.coordinator.stages import read_state, run_stage, session_locks
from qtb.errors import Precondition, Usage
from stage_world import QUIET, chain, compile_, status, verdict


@pytest.fixture
def wheels(monkeypatch):
    """The running harness's wheel, without a real ``uv build``."""
    built = []

    def build(wheels, directory, log_path=None):
        wheels.mkdir(parents=True, exist_ok=True)
        wheel = wheels / "qtb-0-py3-none-any.whl"
        wheel.write_bytes(b"harness")
        built.append(wheel)
        return wheel

    monkeypatch.setattr("qtb.coordinator.build_harness_wheel", build)
    return built


def pin_to_an_older_harness(root):
    run = read_json(root / "run.json")
    run["hashes"].update(coordinator="old-coordinator", implementation="old-implementation")
    write_json(root / "run.json", run)


def failed_cost(world):
    root, _ = compile_(world)
    chain(root, ("quality", "correctness", "unit-tests"))
    world.knobs.fail_once.add("cost")
    assert run_stage(root, "cost", progress=QUIET) == 40
    assert status(root, "cost") == "failed"
    return root


def test_effective_applies_requirements_everywhere_and_hashes_only_to_their_stage():
    run = {
        "hashes": {"coordinator": "a", "implementation": "b", "harness": "w"},
        "cost_evidence": {"identity": "old"},
        "amendments": [
            {
                "stage": "cost",
                "hashes": {"coordinator": "c", "implementation": "d"},
                "session": {"cost_evidence": {"identity": "new"}},
            }
        ],
    }
    cost = effective(run, "cost")
    assert cost["hashes"] == {"coordinator": "c", "implementation": "d", "harness": "w"}
    assert cost["cost_evidence"] == {"identity": "new"}
    for stage in (None, "quality"):
        view = effective(run, stage)
        assert view["hashes"]["coordinator"] == "a" and view["cost_evidence"]["identity"] == "new"
    assert run["hashes"]["coordinator"] == "a"  # the record itself is untouched
    assert effective({"hashes": {}}) == {"hashes": {}}


def test_an_unfinished_cost_is_re_pinned_and_audited(world, wheels):
    root = failed_cost(world)
    pin_to_an_older_harness(root)
    with pytest.raises(Precondition, match="Harness code changed"):
        run_stage(root, "cost", progress=QUIET)

    amendment = amend_cost(root, {}, "the monitor aborted on setup noise", progress=QUIET)
    assert amendment["index"] == 1 and amendment["stage"] == "cost"
    assert amendment["previous"]["hashes"] == {
        "coordinator": "old-coordinator",
        "implementation": "old-implementation",
    }
    assert amendment["previous"]["cost_status"] == "failed"
    assert (root / amendment["wheel"]) == wheels[0]
    run = read_json(root / "run.json")
    assert run["hashes"]["coordinator"] == "old-coordinator"  # only appended to
    assert run["amendments"] == [amendment] and run["format"] == "qtb-run/4"
    assert amendment["previous"]["format"] == "qtb-run/3"
    assert read_json(root / "stages/cost/amendment-1.json") == amendment

    assert run_stage(root, "cost", progress=QUIET) == 0
    # Only cost was re-pinned.
    assert effective(run, "quality")["hashes"]["coordinator"] == "old-coordinator"
    decision = verdict(root)[1]
    assert decision["status"] == "PASS"
    assert any("Amendment 1" in note and "setup noise" in note for note in decision["notes"])
    assert notes(run)[0].startswith("Amendment 1")

    with pytest.raises(Precondition, match="already complete"):
        amend_cost(root, {}, "again", progress=QUIET)


def test_an_amendment_needs_a_reason_and_something_to_change(world, wheels):
    root = failed_cost(world)
    with pytest.raises(Usage, match="reason"):
        amend_cost(root, {}, "  ", progress=QUIET)
    with pytest.raises(Precondition, match="nothing to amend"):
        amend_cost(root, {}, "no change", progress=QUIET)
    assert not wheels and "amendments" not in read_json(root / "run.json")


def test_only_cost_can_be_re_pinned(world, wheels):
    root, _ = compile_(world)
    pin_to_an_older_harness(root)
    with pytest.raises(Precondition, match="quality is not started"):
        amend_cost(root, {}, "too early", progress=QUIET)
    assert not wheels


def test_an_amendment_waits_for_running_stages(world, wheels):
    root = failed_cost(world)
    pin_to_an_older_harness(root)
    with session_locks(root, "cost"):
        with pytest.raises(Precondition, match="amend later"):
            amend_cost(root, {}, "busy", progress=QUIET)
    assert read_state(root, "cost")["status"] == "failed"
