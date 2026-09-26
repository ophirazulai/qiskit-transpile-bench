"""Quiet-core placement and interference monitoring for the monitored cost stage.

The cost job holds nine exclusive physical cores (``affinity[core(1,exclusive=(core,
alljobs))]`` with ``-n 9``). Before measuring, ``CostMonitor.prepare`` checks the
allocation, reads the physical-core topology, pins this coordinator process (and the
monitor, which runs in it) to the first core, reserves the second for the serial cost
worker, and probes that worker core while nothing timed runs. The other seven stay idle.

During measurement the monitor observes every actual worker process through the harness's
worker lifecycle hooks, in bounded windows from launch to exit (``window_s``), and applies
two checks to every window:

- **A. Foreign CPU activity**: busy time of the worker core, all SMT siblings included, less
  the CPU time of the worker and its descendants over the same window, as a fraction of
  physical-core time.
- **B. Involuntary preemption**: the worker's involuntary context switches over the same
  window, summed over all its threads and live descendants, per second.

The first window that fails either check aborts the measurement: the worker is stopped and
reaped and ``Contaminated`` is raised, which ends the stage ``noisy``. Missing samples,
invalid counters or a changed CPU mask are never clean: they raise ``MonitorFailure``. The
checks detect CPU interference; they do not prove the absence of shared-cache,
memory-bandwidth, frequency or thermal effects.

The thresholds are frozen with the contract (``CONTRACT``). The values are IOCR's starting
points and must be calibrated on the approved hardware tier; changing them is a new
contract version.
"""

import math
import os
import resource
import time
from functools import partial
from pathlib import Path

from qtb.canonical import write_json
from qtb.errors import Contaminated, HarnessError, Precondition

from lsf import logging as log
from lsf import measurement_identity
from lsf.context import allocation_problems, parse_cpu_list

CONTRACT = "qtb-lsf-monitor/1"
EVIDENCE_FORMAT = "qtb-lsf-monitor-evidence/1"
COST_SLOTS = 9
LAYOUT = "monitor=core0,worker=core1,reserved=core2-8"
THREAD_SCOPE = (
    "all worker threads and live descendants; the final window is completed from "
    "getrusage(RUSAGE_CHILDREN) of the reaped worker"
)
THRESHOLDS = {
    # Bounded windows during a worker's life, and the idle probe before measuring.
    "window_s": 2.0,
    "idle_probe_s": 2.0,
    # A: foreign busy time on the worker core as a fraction of physical-core time, with a
    # floor for clock-tick granularity in short windows.
    "foreign_cpu_fraction": 0.05,
    "foreign_cpu_floor_s": 0.03,
    # B: involuntary switches of the serial worker per second, with a floor for short
    # windows. Not multiplied by the nine allocated slots.
    "involuntary_per_s": 4.0,
    "involuntary_floor": 2,
}


class MonitorFailure(HarnessError):
    """The monitor cannot vouch for a measurement (missing or invalid samples, moved masks)."""


def check_a(foreign_s, seconds, thresholds):
    limit = max(thresholds["foreign_cpu_fraction"] * seconds, thresholds["foreign_cpu_floor_s"])
    return foreign_s <= limit, limit


def check_b(involuntary, seconds, thresholds):
    limit = max(thresholds["involuntary_per_s"] * seconds, thresholds["involuntary_floor"])
    return involuntary <= limit, limit


def judge(window, thresholds):
    """Both checks from a window's raw counters; the reasons it fails, empty when clean."""
    seconds = window["seconds"]
    reasons = []
    ok, limit = check_a(window["foreign_s"], seconds, thresholds)
    if not ok:
        reasons.append(
            f"A: foreign CPU {window['foreign_s']:.3f} s > {limit:.3f} s "
            f"({thresholds['foreign_cpu_fraction']:.0%} of {seconds:.2f} s)"
        )
    ok, limit = check_b(window["involuntary"], seconds, thresholds)
    if not ok:
        reasons.append(
            f"B: {window['involuntary']} involuntary switches > {limit:.1f} "
            f"({thresholds['involuntary_per_s']:g}/s over {seconds:.2f} s)"
        )
    return reasons


class LinuxProbe:
    """Counters and placement from Linux ``/proc`` and ``/sys``."""

    def __init__(self, proc="/proc", cpus="/sys/devices/system/cpu"):
        self.proc, self.cpus = Path(proc), Path(cpus)
        self.tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

    def unavailable(self):
        if not hasattr(os, "sched_setaffinity") or not (self.proc / "stat").exists():
            return "per-core counters and CPU affinity need Linux /proc and sched_setaffinity"
        status = self.proc / "self" / "status"
        if "nonvoluntary_ctxt_switches" not in status.read_text(errors="replace"):
            return "involuntary context-switch counters are unavailable"
        return None

    def now(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)

    def own_pid(self):
        return os.getpid()

    def online_cpus(self):
        return parse_cpu_list((self.cpus / "online").read_text()) or []

    def siblings(self, cpu):
        path = self.cpus / f"cpu{cpu}" / "topology" / "thread_siblings_list"
        try:
            return parse_cpu_list(path.read_text())
        except OSError:
            return None

    def cpu_model(self):
        try:
            for line in (self.proc / "cpuinfo").read_text(errors="replace").splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return None

    def affinity(self, tid=0):
        return set(os.sched_getaffinity(tid))

    def set_affinity(self, tid, cpus):
        os.sched_setaffinity(tid, set(cpus))

    def tasks(self, pid):
        return sorted(int(name) for name in os.listdir(self.proc / str(pid) / "task"))

    def spawn_options(self, cpus):
        # Placed before exec, so no worker thread ever starts on another core.
        return {"preexec_fn": partial(os.sched_setaffinity, 0, set(cpus))}

    def cpu_busy(self, cpus):
        """Busy seconds of ``cpus`` since boot: everything except idle and iowait."""
        wanted, busy = {f"cpu{c}" for c in cpus}, 0
        for line in (self.proc / "stat").read_text().splitlines():
            name, *values = line.split()
            if name in wanted:
                wanted.discard(name)
                user, nice, system, _idle, _iowait, irq, softirq, steal = map(int, values[:8])
                busy += user + nice + system + irq + softirq + steal
        if wanted:
            raise MonitorFailure(f"/proc/stat has no counters for {sorted(wanted)}")
        return busy / self.tick

    def _stat(self, pid):
        text = (self.proc / str(pid) / "stat").read_text()
        return text[text.rindex(")") + 2 :].split()

    def descendants(self, pid):
        parents = {}
        for entry in self.proc.iterdir():
            if entry.name.isdigit():
                try:
                    parents[int(entry.name)] = int(self._stat(entry.name)[1])
                except (OSError, ValueError, IndexError):
                    continue
        found, frontier = [], [pid]
        while frontier:
            parent = frontier.pop()
            children = [child for child, owner in parents.items() if owner == parent]
            found += children
            frontier += children
        return found

    def process_cpu(self, pid):
        """``{pid: CPU seconds}`` of ``pid`` and each live descendant, threads and reaped
        children included (utime + stime + cutime + cstime)."""
        seconds = {}
        for process in [pid, *self.descendants(pid)]:
            try:
                fields = self._stat(process)
            except OSError:
                if process == pid:
                    raise
                continue
            seconds[process] = sum(int(value) for value in fields[11:15]) / self.tick
        return seconds

    def involuntary(self, pid):
        """``{"pid/tid": switches}`` for every live thread of ``pid`` and its descendants."""
        counts = {}
        for process in [pid, *self.descendants(pid)]:
            try:
                tids = self.tasks(process)
            except OSError:
                if process == pid:
                    raise
                continue
            for tid in tids:
                path = self.proc / str(process) / "task" / str(tid) / "status"
                try:
                    text = path.read_text()
                except OSError:
                    continue
                for line in text.splitlines():
                    if line.startswith("nonvoluntary_ctxt_switches:"):
                        counts[f"{process}/{tid}"] = int(line.split()[1])
                        break
                else:
                    raise MonitorFailure(f"missing involuntary counter for {process}/{tid}")
        return counts

    def children_usage(self):
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return usage.ru_utime + usage.ru_stime, usage.ru_nivcsw


def _finite(*values):
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


class CostMonitor:
    """The cost stage's measurement extension (``qtb.execution.Execution.cost_monitor``)."""

    mode = "cores"
    contract = CONTRACT

    def __init__(self, context, *, tier, attempt, diagnostics, probe=None, thresholds=None):
        self.context = context
        self.tier = dict(tier or {})
        self.attempt = attempt
        self.diagnostics = Path(diagnostics)
        self.probe = probe or LinuxProbe()
        self.thresholds = dict(thresholds or THRESHOLDS)
        self.identity = measurement_identity()
        self.epoch = self.probe.now()
        self.layout = None
        self.preflight = None
        self.active = None
        self.processes = 0
        self._bundles = {}
        self._current = None
        self._deferred = []

    def clock(self):
        return self.probe.now() - self.epoch

    # Before measuring

    def prepare(self, comparison):
        """Allocation, topology, placement and the idle probe; Precondition or Contaminated."""
        self._check_requirement(comparison.run.get("cost_evidence"))
        problems = allocation_problems(
            self.context, slots=COST_SLOTS, exclusive_cores=True, tier=self.tier
        )
        unavailable = self.probe.unavailable()
        if unavailable:
            problems.append(unavailable)
        if not problems:
            try:
                self.layout = self._layout()
                machine = self._machine()
                self._pin_coordinator()
            except Precondition as exc:
                problems.append(str(exc))
        if problems:
            log.error(
                "monitor.refused", "the allocation cannot host monitored cost", problems=problems
            )
            raise Precondition(
                "Monitored cost cannot run in this allocation: " + "; ".join(problems)
            )
        self.preflight = {
            "machine": machine,
            "layout": self.layout,
            "allocation": self.allocation(),
            "tier": self.tier,
            "thresholds": self.thresholds,
        }
        log.event(
            "monitor.placement",
            f"monitor core {self.layout['monitor']}, worker core {self.layout['worker']}",
            layout=self.layout,
            machine=machine,
        )
        idle = self._idle_probe()
        self.preflight["idle_probe"] = idle
        write_json(self.diagnostics / "preflight.json", self.preflight)
        if idle["verdict"] != "clean":
            path = self.diagnostics / "contamination.json"
            evidence = {
                "attempt": self.attempt,
                "host": self.context.get("host"),
                "phase": "idle probe",
                "window": idle,
                "diagnostics": str(path),
            }
            write_json(path, dict(evidence, preflight=self.preflight))
            log.warning("monitor.contaminated", "the idle probe saw foreign activity", **evidence)
            raise Contaminated(
                f"idle probe: foreign CPU {idle['foreign_s']:.3f} s on the worker core in "
                f"{idle['seconds']:.2f} s (limit {idle['limit_s']:.3f} s)",
                evidence,
            )

    def _check_requirement(self, requirement):
        if not requirement:
            return
        from lsf.cost_evidence import requirement_problems

        problems = requirement_problems(requirement, self)
        if problems:
            raise Precondition(
                "This monitor cannot produce the session's cost evidence: " + "; ".join(problems)
            )

    def _layout(self):
        probe = self.probe
        mask = sorted(probe.affinity(0))
        online = probe.online_cpus()
        if online and set(mask) >= set(online):
            raise Precondition(f"not bound: the CPU mask is every CPU of the host ({len(mask)})")
        cores = {}
        for cpu in mask:
            siblings = probe.siblings(cpu)
            if not siblings or cpu not in siblings:
                raise Precondition(f"the topology of CPU {cpu} is unreadable")
            cores[tuple(siblings)] = True
        partial_cores = [list(core) for core in cores if not set(core) <= set(mask)]
        if partial_cores:
            raise Precondition(
                f"the CPU mask holds only part of the physical cores {partial_cores}: "
                "their SMT siblings are free for other jobs"
            )
        if len(cores) != COST_SLOTS:
            raise Precondition(
                f"the CPU mask covers {len(cores)} physical cores, not the {COST_SLOTS} "
                "exclusive cores of the allocation"
            )
        for field in ("bind_cpus", "affinity_file_cpus"):
            granted = self.context.get(field)
            if granted and set(granted) != set(mask):
                raise Precondition(f"the CPU mask {mask} differs from LSF's {field} {granted}")
        ordered = sorted(cores, key=min)
        return {
            "name": LAYOUT,
            "mask": mask,
            "monitor": list(ordered[0]),
            "worker": list(ordered[1]),
            "reserved": [list(core) for core in ordered[2:]],
        }

    def _machine(self):
        probe = self.probe
        online = probe.online_cpus()
        cores = {tuple(s) for s in (probe.siblings(cpu) for cpu in online) if s}
        machine = {
            "host": self.context.get("host"),
            "cpu": probe.cpu_model(),
            "logical_cpus": len(online),
            "physical_cores": len(cores),
        }
        wanted = self.tier.get("ncpus")
        if wanted is not None and machine["physical_cores"] != wanted:
            raise Precondition(
                f"the host has {machine['physical_cores']} physical cores; the approved "
                f"hardware tier has {wanted}"
            )
        return machine

    def _pin_coordinator(self):
        monitor = set(self.layout["monitor"])
        pid = self.probe.own_pid()
        for tid in self.probe.tasks(pid):
            self.probe.set_affinity(tid, monitor)
        self._verify(pid, monitor, "coordinator", precondition=True)

    def _verify(self, pid, cpus, who, precondition=False):
        if who == "worker":
            for child in getattr(self.probe, "descendants", lambda _: [])(pid):
                self._verify(child, cpus, "worker descendant", precondition=precondition)
        for tid in self.probe.tasks(pid):
            try:
                mask = self.probe.affinity(tid)
            except OSError:  # the thread ended since it was listed
                continue
            if mask != set(cpus):
                problem = f"{who} thread {tid} runs on CPUs {sorted(mask)}, not {sorted(cpus)}"
                raise (Precondition if precondition else MonitorFailure)(problem)

    def _idle_probe(self):
        worker = self.layout["worker"]
        start, before = self.clock(), self.probe.cpu_busy(worker)
        self.probe.sleep(self.thresholds["idle_probe_s"])
        end, after = self.clock(), self.probe.cpu_busy(worker)
        foreign, seconds = after - before, end - start
        if not _finite(foreign, seconds) or seconds <= 0 or foreign < 0:
            raise MonitorFailure("the idle probe read invalid counters")
        ok, limit = check_a(foreign, seconds, self.thresholds)
        probe = {
            "start": start,
            "end": end,
            "seconds": seconds,
            "foreign_s": foreign,
            "limit_s": limit,
            "verdict": "clean" if ok else "contaminated",
        }
        log.event("monitor.idle_probe", f"idle probe {probe['verdict']}", **probe)
        return probe

    def allocation(self):
        keys = (
            "job_id",
            "queue",
            "slots",
            "hosts",
            "resource_request",
            "request_source",
            "exclusive_cores_requested",
            "single_host_requested",
            "selectors",
            "bind_cpus",
            "affinity_file_cpus",
        )
        return {key: self.context.get(key) for key in keys}

    # During measurement

    def begin_bundle(self, session):
        if self.layout is None:
            raise MonitorFailure("cost measurement started before the monitor's pre-flight")
        self._bundles[session] = []
        self._current = session

    def worker_hooks(self, label):
        return WorkerHooks(self, label, self._bundles[self._current])

    def end_bundle(self, session):
        """The bundle's monitoring evidence; checked again by ``lsf.cost_evidence``."""
        workers = self._bundles.pop(session)
        return {
            "format": EVIDENCE_FORMAT,
            "contract": CONTRACT,
            "identity": self.identity,
            "attempt": self.attempt,
            "host": self.context.get("host"),
            "machine": self.preflight["machine"],
            "allocation": self.allocation(),
            "layout": self.layout,
            "tier": self.tier,
            "thresholds": self.thresholds,
            "thread_scope": THREAD_SCOPE,
            "idle_probe": self.preflight["idle_probe"],
            "workers": workers,
        }

    def defer(self, name, message, **fields):
        """Monitoring logs wait until the worker is reaped: none inside measured intervals."""
        self._deferred.append((name, message, fields))

    def flush_log(self):
        for name, message, fields in self._deferred:
            log.debug(name, message, **fields)
        self._deferred.clear()

    def contaminated(self, hooks, window, reasons):
        path = self.diagnostics / f"contamination-{hooks.record['process']}.json"
        evidence = {
            "attempt": self.attempt,
            "host": self.context.get("host"),
            "job": hooks.label,
            "pid": hooks.pid,
            "window": window,
            "reasons": reasons,
            "diagnostics": str(path),
        }
        write_json(path, dict(evidence, record=hooks.record, preflight=self.preflight))
        self.flush_log()
        log.warning("monitor.contaminated", "; ".join(reasons), **evidence)
        return Contaminated(
            f"interference on the measurement core during {hooks.label}: " + "; ".join(reasons),
            evidence,
        )


class WorkerHooks:
    """The harness's worker lifecycle hooks for one cost job (``qtb.coordinator.process``)."""

    def __init__(self, monitor, label, records):
        self.monitor, self.label, self.records = monitor, label, records
        self.pid = None
        self.record = None
        self.window = None
        self.final = None
        self.aborted = False
        self.entries = 0

    @property
    def cpus(self):
        return self.monitor.layout["worker"]

    def spawn_options(self):
        return self.monitor.probe.spawn_options(self.cpus)

    def _sample(self, now):
        probe = self.monitor.probe
        try:
            return {
                "time": now,
                "busy": probe.cpu_busy(self.cpus),
                "cpu": probe.process_cpu(self.pid),
                "involuntary": probe.involuntary(self.pid),
                "entries": self.entries,
            }
        except (OSError, ValueError, IndexError) as exc:
            raise MonitorFailure(f"missing monitor sample for {self.label}: {exc}") from exc

    def launched(self, pid):
        monitor = self.monitor
        if monitor.active is not None:
            raise MonitorFailure("two cost workers overlap: the serial protocol was broken")
        monitor.active = self
        monitor.processes += 1
        self.pid, self.entries = pid, 0
        self.final = None
        self.aborted = False
        self.measuring = None
        self.usage = monitor.probe.children_usage()
        now = monitor.clock()
        self.record = {
            "job": self.label,
            "process": monitor.processes,
            "pid": pid,
            "launched": now,
            "exited": None,
            "returncode": None,
            "aborted": False,
            "cpus": self.cpus,
            "windows": [],
            "measurements": [],
        }
        self.records.append(self.record)
        monitor._verify(pid, self.cpus, "worker")
        self.window = self._sample(now)

    def _close(self, end, final):
        """One window's deltas, isolated per actual process and thread.

        A descendant that disappeared was reaped into its parent's child time, which now
        includes what earlier windows already counted for it; that part is taken off. A
        thread that ended takes its last few switches with it; the final window is
        completed from the reaped worker's exact usage.
        """
        start = self.window
        seconds = end["time"] - start["time"]
        busy = end["busy"] - start["busy"]
        before, after = start["cpu"], end["cpu"]
        cpu = sum(value - before.get(pid, 0) for pid, value in after.items())
        cpu -= sum(value for pid, value in before.items() if pid not in after and pid != self.pid)
        counts_before, counts_after = start["involuntary"], end["involuntary"]
        deltas = [value - counts_before.get(key, 0) for key, value in counts_after.items()]
        involuntary = sum(deltas)
        if not _finite(seconds, busy, cpu, involuntary) or seconds <= 0:
            raise MonitorFailure(f"invalid monitor window for {self.label}")
        if (
            busy < 0
            or any(d < 0 for d in deltas)
            or after.get(self.pid, 0) < before.get(self.pid, 0)
        ):
            raise MonitorFailure(f"a monitor counter went backwards for {self.label}")
        if self.measuring is not None and counts_before.keys() - counts_after.keys():
            raise MonitorFailure(f"thread counters disappeared during measured work: {self.label}")
        cpu = max(cpu, 0.0)
        window = {
            "index": len(self.record["windows"]),
            "start": start["time"],
            "end": end["time"],
            "seconds": seconds,
            "busy_s": busy,
            "worker_cpu_s": cpu,
            "foreign_s": busy - cpu,
            "involuntary": involuntary,
            "entries": [start["entries"], end["entries"]],
            "final": final,
            "measurement": self.measuring,
        }
        self.window = end
        return window

    def measurement(self, pid, event):
        """Split at acknowledged worker boundaries, after warmup and before reporting.

        The worker waits outside its timers while we take the boundary sample. Thus a
        measured window never borrows its denominator from imports, setup or warmup.
        """
        if event not in (b"B", b"E") or (event == b"B") != (self.measuring is None):
            raise MonitorFailure(f"invalid measurement boundary for {self.label}")
        monitor = self.monitor
        monitor._verify(pid, self.cpus, "worker")
        monitor._verify(monitor.probe.own_pid(), monitor.layout["monitor"], "coordinator")
        now = monitor.clock()
        sample = self._sample(now)
        problem = None
        if now > self.window["time"]:
            problem = self._judge(self._close(sample, final=False))
        if problem is not None:
            return problem
        if event == b"B":
            self.measuring = len(self.record["measurements"])
            self.record["measurements"].append({"start": now, "end": None})
        else:
            self.record["measurements"][self.measuring]["end"] = now
            self.measuring = None
        return None

    def _judge(self, window):
        monitor = self.monitor
        reasons = judge(window, monitor.thresholds)
        window["a"] = "fail" if any(r.startswith("A:") for r in reasons) else "pass"
        window["b"] = "fail" if any(r.startswith("B:") for r in reasons) else "pass"
        self.record["windows"].append(window)
        monitor.defer(
            "monitor.window",
            f"{self.label} window {window['index']}: A {window['a']}, B {window['b']}",
            job=self.label,
            window=window["index"],
            counters=window,
        )
        if not reasons:
            return None
        self.aborted = self.record["aborted"] = True
        return monitor.contaminated(self, window, reasons)

    def progress(self, pid, completed):
        self.entries = completed
        monitor = self.monitor
        now = monitor.clock()
        if now - self.window["time"] < monitor.thresholds["window_s"]:
            return None
        monitor._verify(pid, self.cpus, "worker")
        monitor._verify(monitor.probe.own_pid(), monitor.layout["monitor"], "coordinator")
        return self._judge(self._close(self._sample(now), final=False))

    def exited(self, pid):
        # Not reaped yet: the thread group's CPU time is still readable.
        self.final = self._close(self._sample(self.monitor.clock()), final=True)
        return None

    def reaped(self, pid, returncode):
        monitor = self.monitor
        monitor.active = None
        cpu, involuntary = (
            after - before
            for after, before in zip(monitor.probe.children_usage(), self.usage, strict=True)
        )
        self.record.update(
            exited=monitor.clock(),
            returncode=returncode,
            rusage={"cpu_s": cpu, "involuntary": involuntary},
        )
        if self.aborted:
            monitor.flush_log()
            return None
        if self.final is None:
            raise MonitorFailure(f"no final monitor window for {self.label}")
        final = self.final
        # Threads that ended are missing from /proc; the reaped worker's usage is exact.
        counted = sum(w["involuntary"] for w in self.record["windows"])
        final["involuntary"] = max(final["involuntary"], involuntary - counted)
        self.record["exited"] = final["end"]
        problem = self._judge(final)
        monitor.flush_log()
        return problem
