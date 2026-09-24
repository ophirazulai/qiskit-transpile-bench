from types import SimpleNamespace

import pytest

from qtb.coordinator import calibration
from qtb.errors import Incomplete


@pytest.mark.parametrize("phase", ["quality", "cost"])
def test_preflight_labels_incomplete_calibration_phase(monkeypatch, tmp_path, phase):
    comparison = SimpleNamespace(
        root=tmp_path,
        directory=tmp_path / "run",
        records=[],
        run={"builds": {}, "scope": {}, "profile": "iterations-profile"},
        policy={"measurement_protocol": {}},
        fixtures=tmp_path,
        quality=lambda *_args, **_kwargs: None,
        progress=lambda *_args: None,
    )
    comparison.directory.mkdir()
    comparison.audit = lambda *_args, **_kwargs: comparison.records.append(
        {"id": "audit/determinism", "result": "passed"}
    )
    monkeypatch.setattr(calibration, "calibration_key", lambda *_: "key")
    if phase == "quality":
        def failed_quality(*_args):
            raise Incomplete("missing baseline block")

        monkeypatch.setattr(calibration, "calibrate_quality", failed_quality)
    else:
        monkeypatch.setattr(
            calibration, "calibrate_quality",
            lambda *_: {"freeze_allowed": True},
        )
        monkeypatch.setattr(calibration, "freeze_roles", lambda *_: [])
        monkeypatch.setattr(
            calibration, "cost_panels", lambda *_args, **_kwargs: {"timing": ([], "timing")}
        )

        def failed_cost(*_args):
            raise Incomplete("cost worker timed out")

        monkeypatch.setattr(calibration, "calibrate_panel", failed_cost)
    with pytest.raises(calibration.CalibrationIncomplete) as exc:
        calibration.preflight(comparison, [])
    assert exc.value.phase == phase
    detail = "missing baseline block" if phase == "quality" else "timing: cost worker timed out"
    assert detail in str(exc.value)
