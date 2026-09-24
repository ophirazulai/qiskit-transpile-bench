"""Expensive baseline calibration survives presentation changes, not semantic ones."""

from copy import deepcopy
from types import SimpleNamespace

from qtb.coordinator import calibration


def comparison():
    return SimpleNamespace(
        run={
            "builds": {"baseline": {"id": "baseline-build"}},
            "hashes": {
                "manifest": "manifest",
                "policy": "policy",
                "harness": "wheel-one",
                "coordinator": "coordinator-one",
                "implementation": "all-code-one",
            },
            "machine": {"cpu": "test"},
        }
    )


def test_calibration_key_reuses_across_nonsemantic_harness_changes(monkeypatch):
    monkeypatch.setattr(calibration, "calibration_implementation_identity", lambda: "measured-code")
    original = comparison()
    key = calibration.calibration_key(original)
    changed = deepcopy(original)
    changed.run["hashes"].update(
        harness="wheel-two", coordinator="coordinator-two", implementation="all-code-two"
    )
    assert calibration.calibration_key(changed) == key
    for field in ("manifest", "policy"):
        changed = deepcopy(original)
        changed.run["hashes"][field] = "changed"
        assert calibration.calibration_key(changed) != key
    changed = deepcopy(original)
    changed.run["builds"]["baseline"]["id"] = "changed"
    assert calibration.calibration_key(changed) != key
    changed = deepcopy(original)
    changed.run["machine"] = {"cpu": "other"}
    assert calibration.calibration_key(changed) != key
    monkeypatch.setattr(calibration, "calibration_implementation_identity", lambda: "changed")
    assert calibration.calibration_key(original) != key


def test_calibration_fingerprint_excludes_reporter_and_includes_worker(monkeypatch):
    paths = []

    def fake_hash(path):
        paths.append(str(path))
        return "0" * 64

    monkeypatch.setattr(calibration, "file_hash", fake_hash)
    calibration.calibration_implementation_identity()
    assert any(path.endswith("src/qtb/canonical/__init__.py") for path in paths)
    assert any(path.endswith("worker/qtb_worker/modes.py") for path in paths)
    assert any(path.endswith("src/qtb/evaluator/statistics.py") for path in paths)
    assert not any(path.endswith("src/qtb/reporter.py") for path in paths)
