"""LSF command construction and response parsing, with a fake ``subprocess.run``."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from lsf.scheduler import (
    JobSpec,
    LsfBackend,
    QueryFailed,
    parse_bhist,
    parse_bjobs,
    resource_request,
    wall_seconds,
)


def test_resource_requests_are_fixed_per_kind():
    selector = {"model": "XeonGold6348", "ncpus": 56, "max_r1m": 10}
    assert resource_request("cost", 16, selector) == (
        "select[r1m < 10 && model == XeonGold6348 && ncpus == 56] span[hosts=1] "
        "affinity[core(1,exclusive=(core,alljobs))] rusage[mem=16]"
    )
    assert resource_request("quality", 32.5) == "span[hosts=1] rusage[mem=32.5]"
    spec = JobSpec("cost", "n", ["python"], None, 16, "8:00", "/o/%J.out", selector)
    argv = spec.argv()
    assert argv[argv.index("-n") + 1] == "9" and "-q" not in argv and "-x" not in argv
    assert argv[-1] == "python" and "-r" not in argv


def test_wall_limits_are_lsf_run_limits():
    assert wall_seconds("12:00") == 12 * 3600
    assert wall_seconds("1:30") == 5400
    assert wall_seconds("45") == 45 * 60
    for bad in ("1:75", "x", "-1", ""):
        with pytest.raises(ValueError):
            wall_seconds(bad)


def test_bjobs_json_is_parsed_and_unknown_jobs_are_not_found():
    stdout = json.dumps(
        {
            "RECORDS": [
                {
                    "JOBID": "12",
                    "STAT": "RUN",
                    "EXIT_CODE": "",
                    "EXEC_HOST": "9*node3",
                    "PEND_REASON": "",
                    "JOB_NAME": "qtb-x",
                    "USER": "me",
                },
                {"JOBID": "13", "ERROR": "Job <13> is not found"},
            ]
        }
    )
    running, missing = parse_bjobs(stdout)
    assert (running.state, running.host, running.exit_code, running.terminal) == (
        "RUN",
        "node3",
        None,
        False,
    )
    assert missing.state == "NOTFOUND"
    with pytest.raises(QueryFailed):
        parse_bjobs(json.dumps({"RECORDS": [{"JOBID": "1", "ERROR": "mbatchd down"}]}))


@pytest.mark.parametrize(
    "text, state, code",
    [
        ("... Done successfully. The CPU time used is 3 seconds.", "DONE", 0),
        ("... Exited with exit code 42. The CPU time used", "EXIT", 42),
        ("... Exited by LSF signal 9. The CPU time", "EXIT", 137),
        ("... Completed <exit>; TERM_OWNER: job killed by owner.", "EXIT", None),
    ],
)
def test_bhist_terminal_states(text, state, code):
    status = parse_bhist("7", text)
    assert (status.state, status.exit_code) == (state, code)
    assert parse_bhist("7", "Submitted from host <login1>") is None


def runner(responses):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(returncode=response[0], stdout=response[1], stderr=response[2])

    return run, calls


def test_submission_outcomes_are_explicit():
    spec = JobSpec("quality", "qtb-r-quality-1", ["python", "-m", "x"], "normal", 32, "1:00", "o")
    run, calls = runner(
        [
            (0, "Job <55> is submitted to queue <normal>.\n", ""),
            (255, "", "Bad resource requirement syntax. Job not submitted."),
            subprocess.TimeoutExpired("bsub", 120),
            (0, "", ""),
            (255, "", "Connection reset by peer"),
            (-9, "", ""),
        ]
    )
    backend = LsfBackend(runner=run)
    accepted = backend.submit(spec)
    assert (accepted.outcome, accepted.job_id) == ("accepted", "55")
    assert calls[0][:3] == ["bsub", "-J", "qtb-r-quality-1"]
    assert backend.submit(spec).outcome == "rejected"
    assert backend.submit(spec).outcome == "ambiguous"
    assert backend.submit(spec).outcome == "ambiguous"
    assert backend.submit(spec).outcome == "ambiguous"
    assert backend.submit(spec).outcome == "ambiguous"


def test_query_failures_are_not_disappearance(tmp_path):
    run, _ = runner(
        [
            subprocess.TimeoutExpired("bjobs", 120),
            (255, "", "LSF is down; try later"),
            (0, "not json", ""),
            (255, "", "Job <9> is not found"),
            (0, json.dumps({"RECORDS": [{"JOBID": "9", "STAT": "DONE", "EXIT_CODE": "0"}]}), ""),
        ]
    )
    backend = LsfBackend(runner=run, diagnostics=tmp_path)
    for _ in range(3):
        with pytest.raises(QueryFailed):
            backend.query("9")
    assert backend.query("9").state == "NOTFOUND"
    assert backend.query("9").exit_code == 0


def test_large_responses_go_to_a_diagnostic_file(tmp_path):
    from lsf import logging as log

    log.setup(tmp_path / "logs", "t", "DEBUG", "test", console=False)
    try:
        run, _ = runner([(0, json.dumps({"RECORDS": []}) + " " * 5000, "")])
        LsfBackend(runner=run, diagnostics=tmp_path / "diagnostics").find("qtb-x")
    finally:
        log.close()
    assert len(list((tmp_path / "diagnostics").glob("bjobs-*.txt"))) == 1
