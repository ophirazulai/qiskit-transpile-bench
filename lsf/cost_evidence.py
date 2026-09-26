"""Admission of monitored cost evidence: the session's required evidence extension.

A session created by the LSF launcher records ``requirement(tier)`` as ``run.json:
cost_evidence``. The harness then calls ``validate`` for every cost bundle it would reuse,
judge or replay, including ``decide`` run directly or after ``clean``
(``qtb.extensions``). A bundle is admissible only when its monitoring evidence:

- comes from this contract, the session's frozen measurement code and thresholds, the
  approved hardware tier and the fixed CPU layout, on the host that measured the samples,
  with an allocation that requested exclusive cores on one host;
- has a clean idle probe;
- covers every worker job of the bundle, both arms, each actual process from launch to
  exit in contiguous windows ending with a final one, with finite counters;
- passes checks A and B in every window when they are recomputed from the raw counters.

Saved ``pass`` labels and verdicts are never trusted.
"""

import math

from qtb.errors import Incomplete

from lsf import logging as log
from lsf import measurement_identity
from lsf.cost_monitor import (
    CONTRACT,
    COST_SLOTS,
    EVIDENCE_FORMAT,
    LAYOUT,
    THRESHOLDS,
    check_a,
    judge,
)

VALIDATOR = "lsf.cost_evidence:validate"
ENTRY_POINT = "python -m lsf.job (submitted by the LSF manager)"
# Windows are contiguous: one window's end is the next one's start.
TOLERANCE_S = 1e-6


def requirement(tier):
    """What a new LSF session records as ``run.json:cost_evidence``."""
    return {
        "contract": CONTRACT,
        "validator": VALIDATOR,
        "entry_point": ENTRY_POINT,
        "identity": measurement_identity(),
        "thresholds": dict(THRESHOLDS),
        "layout": LAYOUT,
        "slots": COST_SLOTS,
        "tier": dict(tier),
    }


def requirement_problems(required, monitor):
    """Why ``monitor`` cannot produce evidence that satisfies ``required``."""
    problems = []
    for name, have in (
        ("contract", monitor.contract),
        ("identity", monitor.identity),
        ("thresholds", monitor.thresholds),
        ("tier", monitor.tier),
    ):
        if required.get(name) != have:
            problems.append(
                f"{name} differs ({have!r}, the session requires {required.get(name)!r})"
            )
    return problems


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _expected_jobs(estimator, count, cases):
    arms = ("baseline", "evolved")
    if estimator == "timing":
        return count * len(arms)
    return count * len(cases) * len(arms)


def problems(bundle, required, *, estimator, count, cases):
    """Every reason ``bundle``'s monitoring evidence is inadmissible; empty when admissible."""
    found = []
    if bundle.get("measurement_mode") != "cores":
        return [f"measured in {bundle.get('measurement_mode', 'an unlabelled')} mode, not cores"]
    monitor = bundle.get("monitor")
    if not isinstance(monitor, dict):
        return ["no monitoring evidence"]
    for name, want in (
        ("format", EVIDENCE_FORMAT),
        ("contract", required.get("contract")),
        ("identity", required.get("identity")),
        ("thresholds", required.get("thresholds")),
        ("tier", required.get("tier")),
    ):
        if monitor.get(name) != want:
            found.append(f"{name} {monitor.get(name)!r} is not the session's {want!r}")
    if found:
        return found
    thresholds = monitor["thresholds"]
    host = monitor.get("host")
    if not host or host != (bundle.get("machine") or {}).get("host"):
        found.append(f"monitored host {host} did not measure the samples")
    if not monitor.get("attempt"):
        found.append("no attempt identity")
    allocation = monitor.get("allocation") or {}
    if allocation.get("slots") != required.get("slots", COST_SLOTS):
        found.append(f"allocation of {allocation.get('slots')} slots")
    if not allocation.get("exclusive_cores_requested") or not allocation.get(
        "single_host_requested"
    ):
        found.append("the allocation did not request exclusive cores on one host")
    if len(allocation.get("hosts") or {}) != 1:
        found.append("the allocation did not span exactly one host")
    tier = monitor["tier"]
    machine = monitor.get("machine") or {}
    if tier.get("ncpus") is not None and machine.get("physical_cores") != tier["ncpus"]:
        found.append(f"{machine.get('physical_cores')} physical cores, not the tier's")
    selected = allocation.get("selectors") or {}
    for name, value in tier.items():
        if value is not None and selected.get(name) != value:
            found.append(f"the allocation did not select {name}=={value}")
    layout = monitor.get("layout") or {}
    worker, coordinator = set(layout.get("worker") or ()), set(layout.get("monitor") or ())
    if layout.get("name") != required.get("layout") or not worker or not coordinator:
        found.append("unknown CPU layout")
    elif worker & coordinator or not (worker | coordinator) <= set(layout.get("mask") or ()):
        found.append("the worker and monitor cores are not disjoint cores of the mask")
    idle = monitor.get("idle_probe") or {}
    if not all(_number(idle.get(k)) for k in ("seconds", "foreign_s")) or idle["seconds"] <= 0:
        found.append("no valid idle probe")
    elif idle["foreign_s"] < 0 or not check_a(idle["foreign_s"], idle["seconds"], thresholds)[0]:
        found.append("the idle probe saw foreign activity")
    found += _coverage_problems(bundle, monitor, thresholds, worker, estimator, count, cases)
    return found


def _coverage_problems(bundle, monitor, thresholds, worker_cpus, estimator, count, cases):
    found = []
    jobs = bundle.get("worker_jobs") or []
    labels = [job.get("job") for job in jobs]
    expected = _expected_jobs(estimator, count, cases)
    if len(labels) != expected:
        found.append(f"{len(labels)} worker jobs, not the {expected} measured")
    if len(set(labels)) != len(labels):
        found.append("a worker job appears twice")
    arms = {}
    for job in jobs:
        arms[job.get("arm")] = arms.get(job.get("arm"), 0) + 1
    if set(arms) != {"baseline", "evolved"} or len(set(arms.values())) != 1:
        found.append(f"unbalanced arms {arms}")
    records = monitor.get("workers") or []
    covered = {record.get("job") for record in records}
    missing = [label for label in labels if label not in covered]
    if missing:
        found.append(f"no monitoring of {len(missing)} worker jobs (first {missing[0]})")
    extra = covered - set(labels)
    if extra:
        found.append(f"monitoring of jobs outside the bundle: {sorted(extra)[:3]}")
    for record in records:
        found += _record_problems(record, thresholds, worker_cpus)
        if len(found) > 20:
            break
    per_job = len(cases) if estimator == "timing" else 20 if estimator == "companion" else 1
    for label in labels:
        measured = sum(len(r.get("measurements", [])) for r in records if r.get("job") == label)
        if measured != per_job:
            found.append(f"{label}: {measured} measured entries, expected {per_job}")
    return found


def _record_problems(record, thresholds, worker_cpus):
    name = f"{record.get('job')} process {record.get('process')}"
    if record.get("aborted"):
        return [f"{name} was aborted"]
    if set(record.get("cpus") or ()) != worker_cpus:
        return [f"{name} ran outside the worker core"]
    windows = record.get("windows") or []
    launched, exited = record.get("launched"), record.get("exited")
    if not windows or not _number(launched) or not _number(exited):
        return [f"{name} has no complete monitoring"]
    if not isinstance(record.get("returncode"), int):
        return [f"{name} has no exit status"]
    found = []
    if abs(windows[0].get("start", math.inf) - launched) > TOLERANCE_S:
        found.append(f"{name}: the first window does not start at launch")
    if abs(windows[-1].get("end", -math.inf) - exited) > TOLERANCE_S:
        found.append(f"{name}: the last window does not end at exit")
    if not windows[-1].get("final") or any(w.get("final") for w in windows[:-1]):
        found.append(f"{name}: no single final window")
    for previous, window in zip([None, *windows], windows, strict=False):
        values = [window.get(k) for k in ("start", "end", "seconds", "busy_s", "worker_cpu_s")]
        values += [window.get("foreign_s"), window.get("involuntary")]
        if not all(_number(v) for v in values) or window["seconds"] <= 0:
            found.append(f"{name} window {window.get('index')}: invalid counters")
            break
        if any(window[k] < 0 for k in ("busy_s", "worker_cpu_s", "involuntary")):
            found.append(f"{name} window {window.get('index')}: invalid counters")
            break
        if previous is not None and abs(window["start"] - previous["end"]) > TOLERANCE_S:
            found.append(f"{name}: a gap before window {window.get('index')}")
            break
        if (
            abs(window["end"] - window["start"] - window["seconds"]) > TOLERANCE_S
            or abs(window["busy_s"] - window["worker_cpu_s"] - window["foreign_s"]) > TOLERANCE_S
        ):
            found.append(f"{name} window {window.get('index')}: inconsistent counters")
            break
        if window["seconds"] > thresholds["window_s"] * 2 + 1:
            found.append(f"{name} window {window.get('index')}: longer than the bounded window")
            break
        reasons = judge(window, thresholds)
        if reasons:
            found.append(f"{name} window {window['index']}: {'; '.join(reasons)}")
            break
    measurements = record.get("measurements") or []
    for index, interval in enumerate(measurements):
        start, end = interval.get("start"), interval.get("end")
        covered = [w for w in windows if w.get("measurement") == index]
        if (
            not _number(start) or not _number(end) or end <= start
            or not covered
            or abs(covered[0]["start"] - start) > TOLERANCE_S
            or abs(covered[-1]["end"] - end) > TOLERANCE_S
            or any(a["end"] != b["start"] for a, b in zip(covered, covered[1:], strict=False))
        ):
            found.append(f"{name}: incomplete measured interval {index}")
    return found


def validate(bundle, required, *, estimator, count, cases):
    """The extension entry point named by ``VALIDATOR``; ``Incomplete`` when inadmissible."""
    found = problems(bundle, required, estimator=estimator, count=count, cases=cases)
    fields = dict(
        component="evidence",
        session_id=bundle.get("session_id"),
        regime=bundle.get("regime"),
        attempt=((bundle.get("monitor") or {}).get("attempt") or {}).get("job_key"),
    )
    if found:
        log.warning("evidence.rejected", "; ".join(found[:5]), problems=found, **fields)
    else:
        log.debug(
            "evidence.accepted", "monitoring evidence admissible", **summary(bundle), **fields
        )
    if found:
        more = f" (and {len(found) - 3} more)" if len(found) > 3 else ""
        raise Incomplete("Inadmissible monitored cost bundle: " + "; ".join(found[:3]) + more)


def summary(bundle):
    """Worst window metrics of a bundle, for the orchestration report."""
    windows = [w for r in (bundle.get("monitor") or {}).get("workers", []) for w in r["windows"]]
    if not windows:
        return {}
    return {
        "windows": len(windows),
        "max_foreign_fraction": max(w["foreign_s"] / w["seconds"] for w in windows),
        "max_involuntary_per_s": max(w["involuntary"] / w["seconds"] for w in windows),
    }
