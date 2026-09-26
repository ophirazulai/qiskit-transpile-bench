"""Translate an LSF job's environment into a validated, scheduler-independent context.

This is the only place that reads ``LSB_*`` variables. A stage job calls
``from_environment()``, checks the allocation with ``allocation_problems()`` and hands the
harness an ``Execution`` (``execution_for``): the slots become the worker count, the job
identity becomes the stage's ``scheduler`` record.

The adapter is measurement-facing: the cost monitor reads the exclusive-core request and
the hardware-tier selectors from here, and they become part of every monitored bundle's
evidence. An affinity mask alone does not prove an exclusive reservation, so the scheduler's
request is recorded next to it.
"""

import os
import re
import socket
from pathlib import Path

from qtb.execution import Execution

FORMAT = "qtb-lsf-context/1"
EXCLUSIVE_CORES = re.compile(r"affinity\[core\(1,exclusive=\(core,alljobs\)\)")
SINGLE_HOST = re.compile(r"span\[hosts=1\]")


def parse_cpu_list(text):
    """A Linux cpu list (``0,56``, ``0-3``, ``0-1,8-9``) as a sorted list; ``None`` if invalid."""
    cpus = set()
    for part in (text or "").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        low, _, high = part.partition("-")
        try:
            start, end = int(low), int(high or low)
        except ValueError:
            return None
        if start < 0 or end < start:
            return None
        cpus.update(range(start, end + 1))
    return sorted(cpus) if cpus else None


def _host_slots(environ):
    """``{host: slots}`` from ``LSB_MCPU_HOSTS`` ("hostA 4 hostB 2"), else ``LSB_HOSTS``."""
    words = environ.get("LSB_MCPU_HOSTS", "").split()
    if words and len(words) % 2 == 0 and all(w.isdigit() for w in words[1::2]):
        return {host: int(count) for host, count in zip(words[::2], words[1::2], strict=True)}
    slots = {}
    for host in environ.get("LSB_HOSTS", "").split():
        slots[host] = slots.get(host, 0) + 1
    return slots


def _short(host):
    return host.split(".")[0]


def _affinity_file_cpus(path, host):
    """This host's CPUs from ``LSB_AFFINITY_HOSTFILE`` (lines: ``host cpu_list [...]``)."""
    if not path or not Path(path).is_file():
        return None
    cpus = []
    for line in Path(path).read_text(errors="replace").splitlines():
        words = line.split()
        if len(words) >= 2 and _short(words[0]) == _short(host):
            parsed = parse_cpu_list(words[1])
            if parsed is None:
                return None
            cpus.extend(parsed)
    return sorted(set(cpus)) or None


def selectors(request):
    """The hardware-tier and load selectors of a resource request's ``select[...]``."""
    compact = re.sub(r"\s+", "", request or "")
    terms = {}
    for match in re.finditer(r"select\[(.*?)\](?=\s*[a-z]+\[|$)", compact):
        text = match.group(1)
        model = re.search(r"model==([A-Za-z0-9_.\-]+)", text)
        ncpus = re.search(r"ncpus==(\d+)", text)
        r1m = re.search(r"r1m<([0-9.]+)", text)
        if model:
            terms["model"] = model.group(1)
        if ncpus:
            terms["ncpus"] = int(ncpus.group(1))
        if r1m:
            terms["max_r1m"] = float(r1m.group(1))
    return terms


def from_environment(environ=None, host=None):
    """The allocation this process runs in, as a JSON-serializable record."""
    environ = os.environ if environ is None else environ
    host = host or socket.gethostname()
    slots_text = environ.get("LSB_DJOB_NUMPROC", "")
    request = environ.get("LSB_EFFECTIVE_RSRCREQ") or environ.get("LSB_SUB_RES_REQ") or ""
    compact = re.sub(r"\s+", "", request)
    host_slots = _host_slots(environ)
    return {
        "format": FORMAT,
        "scheduler": "lsf" if environ.get("LSB_JOBID") else None,
        "job_id": environ.get("LSB_JOBID"),
        "job_name": environ.get("LSB_JOBNAME"),
        "queue": environ.get("LSB_QUEUE"),
        "host": host,
        "hosts": host_slots,
        "slots": int(slots_text) if slots_text.isdigit() else None,
        "resource_request": request or None,
        "request_source": (
            "LSB_EFFECTIVE_RSRCREQ"
            if environ.get("LSB_EFFECTIVE_RSRCREQ")
            else "LSB_SUB_RES_REQ"
            if environ.get("LSB_SUB_RES_REQ")
            else None
        ),
        "exclusive_cores_requested": bool(EXCLUSIVE_CORES.search(compact)),
        "single_host_requested": bool(SINGLE_HOST.search(compact)),
        "selectors": selectors(request),
        "bind_cpus": parse_cpu_list(environ.get("LSB_BIND_CPU_LIST")),
        "affinity_file_cpus": _affinity_file_cpus(environ.get("LSB_AFFINITY_HOSTFILE"), host),
    }


def allocation_problems(context, *, slots, exclusive_cores=False, tier=None):
    """Why this allocation cannot run the job as requested; empty when it can."""
    problems = []
    if context.get("scheduler") != "lsf":
        return ["not running inside an LSF job (LSB_JOBID is not set)"]
    if context.get("slots") != slots:
        problems.append(f"LSF granted {context.get('slots')} slots, the job needs {slots}")
    hosts = context.get("hosts") or {}
    if len(hosts) != 1:
        problems.append(f"the allocation spans {len(hosts)} hosts ({sorted(hosts)}), not one")
    elif _short(next(iter(hosts))) != _short(context.get("host") or ""):
        problems.append(f"this process runs on {context.get('host')}, not on {next(iter(hosts))}")
    if not exclusive_cores:
        return problems
    if not context.get("resource_request"):
        problems.append("the job's resource request is unavailable, so exclusivity is unproven")
    else:
        if not context["exclusive_cores_requested"]:
            problems.append(
                "the job did not request exclusive cores "
                "(affinity[core(1,exclusive=(core,alljobs))])"
            )
        if not context["single_host_requested"]:
            problems.append("the job did not request span[hosts=1]")
    requested = context.get("selectors") or {}
    for name, value in (tier or {}).items():
        if value is not None and requested.get(name) != value:
            problems.append(
                f"the request does not select the approved hardware tier {name}=={value} "
                f"(it selects {requested.get(name)})"
            )
    return problems


def scheduler_record(context):
    """What a stage records as its ``scheduler`` field."""
    return {
        "name": context.get("scheduler"),
        "job_id": context.get("job_id"),
        "job_name": context.get("job_name"),
        "queue": context.get("queue"),
        "hosts": context.get("hosts"),
        "slots": context.get("slots"),
    }


def execution_for(context, *, invocation, workers=None, **extensions):
    """The harness's execution context for one stage invocation in this allocation."""
    return Execution(
        scheduler=scheduler_record(context),
        workers=workers,
        invocation=invocation,
        **extensions,
    )
