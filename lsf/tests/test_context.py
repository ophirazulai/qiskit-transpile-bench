"""The allocation adapter: LSF environment to a validated execution context."""

from qtb.coordinator import worker_count
from qtb.execution import installed

from lsf.context import (
    allocation_problems,
    execution_for,
    from_environment,
    parse_cpu_list,
    scheduler_record,
    selectors,
)
from lsf.tests.conftest import COST_REQUEST, HOST, lsf_environment


def test_cpu_lists_parse_or_are_refused():
    assert parse_cpu_list("0,56") == [0, 56]
    assert parse_cpu_list("0-3") == [0, 1, 2, 3]
    assert parse_cpu_list("0-1,8-9") == [0, 1, 8, 9]
    assert parse_cpu_list("") is None
    assert parse_cpu_list("3-1") is None
    assert parse_cpu_list("x") is None


def test_selectors_come_from_the_effective_request():
    assert selectors(COST_REQUEST) == {"max_r1m": 10.0, "ncpus": 56}
    assert selectors("select[model == XeonGold6348 && r1m < 8] span[hosts=1]") == {
        "model": "XeonGold6348",
        "max_r1m": 8.0,
    }
    assert selectors("span[hosts=1]") == {}


def test_environment_becomes_a_scheduler_independent_context(tmp_path):
    hostfile = tmp_path / "affinity"
    hostfile.write_text(f"{HOST}.cluster 0,56,1,57\nother 3\n")
    environ = lsf_environment(
        9,
        COST_REQUEST,
        LSB_BIND_CPU_LIST="0,56,1,57",
        LSB_AFFINITY_HOSTFILE=str(hostfile),
    )
    context = from_environment(environ, host=HOST)
    assert context["scheduler"] == "lsf" and context["slots"] == 9
    assert context["hosts"] == {HOST: 9}
    assert context["exclusive_cores_requested"] and context["single_host_requested"]
    assert context["request_source"] == "LSB_EFFECTIVE_RSRCREQ"
    assert context["bind_cpus"] == [0, 1, 56, 57]
    assert context["affinity_file_cpus"] == [0, 1, 56, 57]
    assert scheduler_record(context) == {
        "name": "lsf",
        "job_id": "4242",
        "job_name": "qtb-test",
        "queue": "normal",
        "hosts": {HOST: 9},
        "slots": 9,
    }
    # Without LSB_MCPU_HOSTS the per-slot host list is counted.
    environ.pop("LSB_MCPU_HOSTS")
    assert from_environment(environ, host=HOST)["hosts"] == {HOST: 9}


def test_allocation_problems_name_each_mismatch():
    assert allocation_problems(from_environment({}, host=HOST), slots=16) == [
        "not running inside an LSF job (LSB_JOBID is not set)"
    ]
    ordinary = from_environment(lsf_environment(16), host=HOST)
    assert allocation_problems(ordinary, slots=16) == []
    assert "granted 16 slots, the job needs 9" in allocation_problems(ordinary, slots=9)[0]
    problems = allocation_problems(ordinary, slots=16, exclusive_cores=True)
    assert any("exclusive cores" in p for p in problems)
    split = from_environment(
        dict(lsf_environment(16), LSB_MCPU_HOSTS=f"{HOST} 8 other 8"), host=HOST
    )
    assert any("spans 2 hosts" in p for p in allocation_problems(split, slots=16))
    cost = from_environment(lsf_environment(9, COST_REQUEST), host=HOST)
    assert allocation_problems(cost, slots=9, exclusive_cores=True, tier={"ncpus": 56}) == []
    wrong_tier = allocation_problems(
        cost, slots=9, exclusive_cores=True, tier={"ncpus": 56, "model": "XeonGold"}
    )
    assert wrong_tier and "model==XeonGold" in wrong_tier[0]
    unknown = from_environment(dict(lsf_environment(9), LSB_EFFECTIVE_RSRCREQ=""), host=HOST)
    assert any("unproven" in p for p in allocation_problems(unknown, slots=9, exclusive_cores=True))


def test_granted_slots_reach_the_harness_only_through_the_execution_context():
    context = from_environment(lsf_environment(16), host=HOST)
    execution = execution_for(context, invocation={"job_key": "k"}, workers=context["slots"])
    with installed(execution):
        assert worker_count() == 16
    assert execution.scheduler["job_id"] == "4242" and execution.invocation == {"job_key": "k"}
