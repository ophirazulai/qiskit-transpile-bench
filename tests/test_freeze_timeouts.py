"""A frozen timeout needs a measured baseline compile for every required seed."""

import importlib.util
from pathlib import Path

import pytest

from qtb.errors import HarnessError


def freeze_module():
    path = Path(__file__).resolve().parents[1] / "tools/freeze_timeouts.py"
    spec = importlib.util.spec_from_file_location("freeze_timeouts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_freeze_uses_slowest_baseline_seed_and_requires_multiplier():
    module = freeze_module()
    manifest = {
        "version": 1,
        "cases": [
            {"case_id": "normal", "role": "scored", "seeds_per_block": 2, "timeout_s": 120},
            {
                "case_id": "confirm/multiplier_h18_n20/target/L2",
                "role": "guard",
                "seeds_per_block": 2,
                "timeout_s": 120,
            },
        ],
    }
    records = [
        {"case_id": "normal", "seed": 0, "status": "ok", "compile_ns": 1_000_000_000},
        {"case_id": "normal", "seed": 1, "status": "ok", "compile_ns": 12_100_000_001},
        {
            "case_id": "confirm/multiplier_h18_n20/target/L2",
            "seed": 0,
            "status": "ok",
            "compile_ns": 13_000_000_000,
        },
        {
            "case_id": "confirm/multiplier_h18_n20/target/L2",
            "seed": 1,
            "status": "ok",
            "compile_ns": 14_000_000_000,
        },
    ]
    with pytest.raises(HarnessError, match="Incomplete baseline"):
        module.freeze(manifest, records[:-1], source_hash="a" * 64)
    # The complete helper also validates the manifest contract; use a real
    # profile below and exercise the derivation through its published cases.
    from qtb.config import load_profile

    full_manifest, _, _ = load_profile("confirm-profile", verify=False)
    multiplier = next(
        c
        for c in full_manifest["cases"]
        if c["input_group"] == "multiplier_h18_n20" and c["role"] == "guard"
    )
    expected = [
        {
            "case_id": multiplier["case_id"],
            "seed": seed,
            "status": "ok",
            "compile_ns": 14_000_000_000,
        }
        for seed in module.required_seeds(multiplier)
    ]
    narrowed = dict(full_manifest, cases=[multiplier])
    frozen = module.freeze(narrowed, expected, source_hash="a" * 64)
    assert frozen["version"] == full_manifest["version"] + 1
    assert "status" not in frozen
    assert frozen["cases"][0]["timeout_s"] == 140
    assert full_manifest["cases"] != frozen["cases"]
