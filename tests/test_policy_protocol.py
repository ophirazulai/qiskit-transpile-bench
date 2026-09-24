from copy import deepcopy

import pytest
import qiskit

from qtb.config import PROTOCOL, data_root, load_profile, validate
from qtb.errors import HarnessError
from qtb_worker.modes import run_seed


def test_timing_worker_obeys_job_protocol(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(qiskit, "transpile", lambda *_args, **_kwargs: calls.append("compile"))
    ticks = iter(range(0, 100, 5))
    monkeypatch.setattr("qtb_worker.modes.time.perf_counter_ns", lambda: next(ticks))
    job = {
        "mode": "timing_e2e",
        "case": {"options": {}, "constraint_form": "target", "optimization_level": 0},
        "warmups": 2,
        "minimum_calls": 4,
        "minimum_ns": 15,
    }

    result = run_seed(job, 0, tmp_path, (object(), object(), {}))

    assert result["samples_ns"] == [5] * 4
    assert len(calls) == 6


def test_policy_and_job_reject_invalid_timing_protocol():
    manifest, policy, _ = load_profile("iterations-profile", verify=False)
    case = manifest["cases"][0]
    job = {
        "protocol": PROTOCOL,
        "mode": "timing_e2e",
        "case": case,
        "fixture_root": str(data_root() / "fixtures"),
        "seeds": [0],
        "build": {},
        "timeout_s": 120,
        "warmups": 2,
        "minimum_calls": 4,
        "minimum_ns": 250,
    }
    validate("job", job)
    bad_job = dict(job, minimum_calls=0)
    with pytest.raises(HarnessError, match="minimum_calls"):
        validate("job", bad_job)
    bad_policy = deepcopy(policy)
    bad_policy["measurement_protocol"]["quality_batch_size"] = 26
    with pytest.raises(HarnessError, match="quality_batch_size"):
        validate("policy", bad_policy)
