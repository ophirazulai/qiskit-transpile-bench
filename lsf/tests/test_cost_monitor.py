"""The cost monitor on synthetic counters and topology."""

from types import SimpleNamespace

import pytest

from qtb.errors import Contaminated, Precondition

from lsf.cost_evidence import requirement
from lsf.cost_monitor import (
    CONTRACT,
    THRESHOLDS,
    CostMonitor,
    LinuxProbe,
    MonitorFailure,
    check_a,
    check_b,
    judge,
)
from lsf.tests.conftest import HOST, SyntheticProbe, cost_context, drive

TIER = {"model": None, "ncpus": 56}


def monitor(tmp_path, probe, tier=TIER, **context):
    return CostMonitor(
        cost_context(**context),
        tier=tier,
        attempt={"job_key": "cost-a01-x", "attempt": 1},
        diagnostics=tmp_path / "diagnostics",
        probe=probe,
    )


def comparison(required=None):
    return SimpleNamespace(run={"cost_evidence": required} if required else {})


def test_checks_have_floors_for_short_windows_and_scale_with_long_ones():
    t = THRESHOLDS
    assert check_a(0.03, 0.1, t)[0] and not check_a(0.031, 0.1, t)[0]
    assert check_a(0.1, 2.0, t)[0] and not check_a(0.11, 2.0, t)[0]
    assert check_b(2, 0.1, t)[0] and not check_b(3, 0.1, t)[0]
    assert check_b(8, 2.0, t)[0] and not check_b(9, 2.0, t)[0]
    window = {"seconds": 2.0, "foreign_s": 0.5, "involuntary": 20}
    reasons = judge(window, t)
    assert [r[:2] for r in reasons] == ["A:", "B:"]
    assert judge(dict(window, foreign_s=0.0), t)[0].startswith("B:")
    assert judge(dict(window, involuntary=0), t)[0].startswith("A:")


def test_preflight_pins_disjoint_full_cores_and_probes_the_idle_worker_core(
    tmp_path, probe, fast_thresholds
):
    m = monitor(tmp_path, probe)
    m.prepare(comparison())
    assert m.layout["monitor"] == [0, 56] and m.layout["worker"] == [1, 57]
    assert [len(core) for core in m.layout["reserved"]] == [2] * 7
    assert probe.affinities[1] == {0, 56}
    assert m.preflight["idle_probe"]["verdict"] == "clean"
    assert m.preflight["machine"]["physical_cores"] == 56
    assert (tmp_path / "diagnostics/preflight.json").exists()


def test_preflight_accepts_a_host_without_smt(tmp_path, fast_thresholds):
    probe = SyntheticProbe(smt=False, cores=56)
    m = monitor(tmp_path, probe)
    m.prepare(comparison())
    assert m.layout["worker"] == [1]


@pytest.mark.parametrize(
    "probe, message",
    [
        (SyntheticProbe(mask=list(range(9)) + list(range(56, 64))), "part of the physical cores"),
        (SyntheticProbe(mask=SyntheticProbe().cpus_of(range(8))), "covers 8 physical cores"),
        (SyntheticProbe(mask=SyntheticProbe().cpus_of(range(56))), "not bound"),
        (SyntheticProbe(cores=48), "48 physical cores"),
    ],
)
def test_preflight_refuses_allocations_it_cannot_vouch_for(tmp_path, probe, message):
    with pytest.raises(Precondition, match=message):
        monitor(tmp_path, probe).prepare(comparison())


def test_preflight_refuses_unreadable_topology_missing_counters_and_foreign_masks(tmp_path):
    probe = SyntheticProbe()
    probe.broken.add("topology")
    with pytest.raises(Precondition, match="topology of CPU 0 is unreadable"):
        monitor(tmp_path, probe).prepare(comparison())
    probe = SyntheticProbe()
    probe.unavailable = lambda: "involuntary context-switch counters are unavailable"
    with pytest.raises(Precondition, match="context-switch counters"):
        monitor(tmp_path, probe).prepare(comparison())
    with pytest.raises(Precondition, match="differs from LSF's bind_cpus"):
        monitor(tmp_path, SyntheticProbe(), LSB_BIND_CPU_LIST="0-8").prepare(comparison())
    ordinary = CostMonitor(
        cost_context(LSB_EFFECTIVE_RSRCREQ="span[hosts=1]"),
        tier=TIER,
        attempt={"job_key": "k"},
        diagnostics=tmp_path,
        probe=SyntheticProbe(),
    )
    with pytest.raises(Precondition, match="did not request exclusive cores"):
        ordinary.prepare(comparison())


def test_preflight_refuses_a_session_that_requires_other_evidence(tmp_path, probe):
    required = dict(requirement(TIER), identity="frozen-elsewhere")
    with pytest.raises(Precondition, match="identity differs"):
        monitor(tmp_path, probe).prepare(comparison(required))
    required = requirement({"model": "XeonGold", "ncpus": 56})
    with pytest.raises(Precondition, match="tier differs"):
        monitor(tmp_path, probe).prepare(comparison(required))


def test_a_noisy_idle_worker_core_is_a_contaminated_landing(tmp_path, fast_thresholds):
    probe = SyntheticProbe(foreign=lambda t: 0.5)
    m = monitor(tmp_path, probe)
    with pytest.raises(Contaminated, match="idle probe") as caught:
        m.prepare(comparison())
    assert caught.value.evidence["phase"] == "idle probe"
    assert (tmp_path / "diagnostics/contamination.json").exists()


def prepared(tmp_path, probe):
    m = monitor(tmp_path, probe)
    m.prepare(comparison())
    m.begin_bundle("session")
    return m


def test_clean_workers_are_covered_launch_to_exit_in_bounded_windows(
    tmp_path, probe, fast_thresholds
):
    m = prepared(tmp_path, probe)
    for label, pid in (("0-batch-baseline", 100), ("0-batch-evolved", 101)):
        assert drive(m.worker_hooks(label), probe, pid, seconds=7.05) is None
    evidence = m.end_bundle("session")
    assert evidence["contract"] == CONTRACT and evidence["host"] == HOST
    first = evidence["workers"][0]
    windows = first["windows"]
    assert len(windows) == 4 and windows[-1]["final"]
    assert windows[0]["start"] == first["launched"] and windows[-1]["end"] == first["exited"]
    assert all(a["end"] == b["start"] for a, b in zip(windows, windows[1:], strict=False))
    assert all(w["seconds"] <= THRESHOLDS["window_s"] + 0.11 for w in windows)
    assert all(w["a"] == w["b"] == "pass" for w in windows)
    assert abs(sum(w["worker_cpu_s"] for w in windows) - first["rusage"]["cpu_s"]) < 1e-6
    assert windows[-1]["entries"][1] == 3


def test_brief_contamination_aborts_on_its_own_window_not_diluted(tmp_path, fast_thresholds):
    # 0.3 s of a foreign process in a 60 s run: 0.5 % overall, 15 % of one window.
    probe = SyntheticProbe(foreign=lambda t: 1.0 if 20.0 <= t < 20.3 else 0.0)
    m = prepared(tmp_path, probe)
    problem = drive(m.worker_hooks("0-batch-baseline"), probe, 100, seconds=60)
    assert isinstance(problem, Contaminated)
    window = problem.evidence["window"]
    assert window["a"] == "fail" and window["b"] == "pass"
    assert window["start"] <= 20.3 and window["end"] >= 20.0
    record = m._bundles["session"][0]
    assert record["aborted"] and len(record["windows"]) < 30
    assert (tmp_path / "diagnostics/contamination-1.json").exists()


def test_preemption_alone_fails_check_b(tmp_path, fast_thresholds):
    probe = SyntheticProbe(preemption=lambda t: 50.0 if t > 5 else 0.5)
    m = prepared(tmp_path, probe)
    problem = drive(m.worker_hooks("0-x-baseline"), probe, 100, seconds=20)
    assert problem.evidence["window"]["a"] == "pass"
    assert problem.evidence["window"]["b"] == "fail"


def test_the_final_window_is_completed_from_the_reaped_workers_usage(
    tmp_path, probe, fast_thresholds
):
    m = prepared(tmp_path, probe)
    hooks = m.worker_hooks("0-x-baseline")
    hooks.spawn_options()
    probe.start(100)
    hooks.launched(100)
    probe.advance(0.5)
    hooks.exited(100)
    # Threads that ended took 40 switches out of /proc; the worker's rusage keeps them.
    probe.switches += 40
    probe.dead_thread_switches = 40
    probe.stop()
    problem = hooks.reaped(100, 0)
    assert isinstance(problem, Contaminated) and "B:" in str(problem)
    assert problem.evidence["window"]["final"]


def test_every_process_of_a_timeout_restart_is_monitored(tmp_path, probe, fast_thresholds):
    m = prepared(tmp_path, probe)
    hooks = m.worker_hooks("0-case-evolved")
    assert drive(hooks, probe, 100, seconds=3) is None
    assert drive(hooks, probe, 101, seconds=3) is None  # the fresh retry process
    records = m.end_bundle("session")["workers"]
    assert [(r["job"], r["process"], r["pid"]) for r in records] == [
        ("0-case-evolved", 1, 100),
        ("0-case-evolved", 2, 101),
    ]


def test_moved_masks_missing_samples_and_overlaps_are_never_clean(tmp_path, probe, fast_thresholds):
    m = prepared(tmp_path, probe)
    hooks = m.worker_hooks("0-x-baseline")
    probe.start(100)
    hooks.spawn_options()
    hooks.launched(100)
    probe.affinities[100] = {1, 2}
    probe.advance(2.5)
    with pytest.raises(MonitorFailure, match="runs on CPUs"):
        hooks.progress(100, 0)
    m = prepared(tmp_path / "b", SyntheticProbe())
    first = m.worker_hooks("a")
    first.spawn_options()
    first.launched(100)
    with pytest.raises(MonitorFailure, match="overlap"):
        m.worker_hooks("b").launched(101)
    probe = SyntheticProbe()
    m = prepared(tmp_path / "c", probe)
    hooks = m.worker_hooks("a")
    hooks.spawn_options()
    probe.start(100)
    hooks.launched(100)
    probe.broken.add("counters")
    probe.advance(2.5)
    with pytest.raises(MonitorFailure, match="missing monitor sample"):
        hooks.progress(100, 0)


def fake_proc(tmp_path):
    proc, cpus = tmp_path / "proc", tmp_path / "cpu"
    (proc / "self").mkdir(parents=True)
    (proc / "stat").write_text(
        "cpu  1 2 3 4 5 6 7 8 0 0\n"
        "cpu0 100 0 50 900 10 5 5 0 0 0\n"
        "cpu1 200 10 20 900 10 0 0 3 7 0\n"
    )
    (proc / "self" / "status").write_text("nonvoluntary_ctxt_switches:\t4\n")

    def process(pid, ppid, times, tasks):
        (proc / str(pid) / "task").mkdir(parents=True)
        fields = ["S", str(ppid)] + ["0"] * 9 + [str(t) for t in times] + ["0"] * 20
        (proc / str(pid) / "stat").write_text(f"{pid} (py thon) " + " ".join(fields))
        for tid, switches in tasks.items():
            (proc / str(pid) / "task" / str(tid)).mkdir()
            (proc / str(pid) / "task" / str(tid) / "status").write_text(
                f"Name:\tpython\nnonvoluntary_ctxt_switches:\t{switches}\n"
            )

    process(10, 1, [100, 50, 7, 3], {10: 5, 11: 2})
    process(12, 10, [20, 10, 0, 0], {12: 1})
    process(13, 99, [999, 999, 0, 0], {13: 100})
    for cpu, siblings in ((0, "0,56"), (1, "1-2")):
        (cpus / f"cpu{cpu}" / "topology").mkdir(parents=True)
        (cpus / f"cpu{cpu}" / "topology" / "thread_siblings_list").write_text(siblings + "\n")
    (cpus / "online").write_text("0-1\n")
    probe = LinuxProbe(proc, cpus)
    probe.tick = 100
    return probe


def test_linux_counters_parse_proc_and_sys(tmp_path):
    probe = fake_proc(tmp_path)
    # user nice system irq softirq steal; idle and iowait are not busy
    assert probe.cpu_busy([0]) == pytest.approx(1.6)
    assert probe.cpu_busy([0, 1]) == pytest.approx(1.6 + 2.33)
    with pytest.raises(Exception, match="no counters"):
        probe.cpu_busy([5])
    assert probe.descendants(10) == [12]
    assert probe.process_cpu(10) == {10: pytest.approx(1.6), 12: pytest.approx(0.3)}
    assert probe.involuntary(10) == {"10/10": 5, "10/11": 2, "12/12": 1}
    assert probe.siblings(0) == [0, 56] and probe.siblings(1) == [1, 2]
    assert probe.siblings(7) is None
    assert probe.online_cpus() == [0, 1]


def test_setup_cannot_dilute_noise_in_a_short_measurement(tmp_path, probe, fast_thresholds):
    m = prepared(tmp_path, probe)
    hooks = m.worker_hooks("short-entry")
    hooks.spawn_options()
    probe.start(100)
    hooks.launched(100)
    probe.advance(1.5)  # clean input loading and warmup
    assert hooks.measurement(100, b"B") is None
    probe.foreign = lambda _: 0.5
    probe.advance(0.1)  # 50 ms foreign work: hidden in a 1.6 s lifetime window
    problem = hooks.measurement(100, b"E")
    assert isinstance(problem, Contaminated)
    assert problem.evidence["window"]["seconds"] == pytest.approx(0.1)
    assert problem.evidence["window"]["measurement"] == 0


def test_missing_thread_counters_are_not_zero_preemption(tmp_path):
    probe = fake_proc(tmp_path)
    (probe.proc / "10/task/11/status").write_text("Name: python\n")
    with pytest.raises(MonitorFailure, match="missing involuntary counter"):
        probe.involuntary(10)


def test_descendants_cannot_escape_the_worker_core(tmp_path, probe, fast_thresholds):
    m = prepared(tmp_path, probe)
    hooks = m.worker_hooks("child")
    hooks.spawn_options()
    probe.descendants = lambda pid: [101] if pid == 100 else []
    probe.affinities[101] = {2, 58}
    probe.start(100)
    with pytest.raises(MonitorFailure, match="worker descendant"):
        hooks.launched(100)
