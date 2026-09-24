"""Scheduled start times must account for frozen target durations."""

from qtb_verifier.schedule import verify_schedule


def target():
    return {
        "dt": (1.0).hex(),
        "timing_constraints": {
            "acquire_alignment": 1,
            "pulse_alignment": 1,
            "granularity": 1,
            "min_length": 1,
        },
        "instructions": [
            {
                "name": "x",
                "properties": [{"qargs": [0], "duration": (10.0).hex()}],
            }
        ],
    }


def test_successor_start_requires_exact_target_duration_or_explicit_delay():
    gate = ["x", [0], [], [], {}]
    delay = ["delay", [0], [], [(10.0).hex()], {"unit": "dt"}]

    # A 10 dt gate followed by an operation at 20 dt leaves an unexplained
    # interval. The old oracle accepted this because it compared the target
    # duration to another value derived from the same target.
    invalid = verify_schedule([gate, gate], [0, 20], [10, 10], target())
    assert invalid["status"] == "mismatch"
    assert "Idle gap without delay" in invalid["errors"]

    valid = verify_schedule([gate, delay, gate], [0, 10, 20], [10, 10, 10], target())
    assert valid["status"] == "verified"
    assert valid["makespan"] == 30

    overlap = verify_schedule([gate, gate], [0, 5], [10, 10], target())
    assert overlap["status"] == "mismatch"
    assert "Overlap or dependency violation" in overlap["errors"]
