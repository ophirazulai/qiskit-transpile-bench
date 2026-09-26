"""The companion orchestration report: ``<session>.lsf/report.md`` and ``report.json``.

It is produced from the ledger alone and lives outside the session, so it survives the
session's cleanup. It links every job and cost attempt to its logs and evidence, shows the
retry budget and says why the pipeline stopped. The verdict itself is in the session's
``report.md`` and ``decision.json``.
"""

from datetime import UTC, datetime

from qtb.canonical import atomic_bytes, write_json

from lsf import retry

FORMAT = "qtb-lsf-report/1"
ORDER = ("compile", "quality", "correctness", "unit-tests", "cost", "clean")


def _job(entry):
    outcome = entry.get("outcome") or {}
    contamination = outcome.get("contamination") or {}
    window = contamination.get("window") or {}
    seconds = window.get("seconds") or 0
    monitor = outcome.get("monitor") or {}

    def rate(value):
        return value / seconds if seconds and isinstance(value, (int, float)) else None

    return {
        "key": entry["key"],
        "stage": entry["stage"],
        "attempt": entry.get("attempt"),
        "name": entry.get("name"),
        "job_id": entry.get("job_id"),
        "status": entry["status"],
        "outcome": outcome.get("kind"),
        "exit_code": outcome.get("exit_code"),
        "host": outcome.get("host"),
        "reason": outcome.get("reason") or outcome.get("message"),
        "consumes_budget": entry.get("consumes_budget"),
        "resources": entry.get("resources"),
        "output": entry.get("output"),
        "logs": outcome.get("logs"),
        "evidence": contamination.get("diagnostics"),
        "phase": contamination.get("phase") or ("measurement" if window else None),
        "foreign_fraction": rate(window.get("foreign_s")),
        "involuntary_per_s": rate(window.get("involuntary")),
        "cpu": (monitor.get("machine") or {}).get("cpu"),
        "layout": monitor.get("layout"),
        "preflight": monitor.get("preflight"),
    }


def build(manager):
    ledger = manager.ledger
    data = ledger.data
    jobs = sorted(
        (_job(e) for e in ledger.jobs() if e["stage"] != "manager"),
        key=lambda j: (ORDER.index(j["stage"]), j["attempt"] or 0, j["key"]),
    )
    decision = retry.decision(ledger)
    return {
        "format": FORMAT,
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": data["run_id"],
        "session": data["session"],
        "stop_reason": manager.stop_reason,
        "managers": data["managers"],
        "jobs": jobs,
        "cost": {
            "attempts": [j for j in jobs if j["stage"] == "cost"],
            "used": decision["used"],
            "limit": decision["limit"],
            "remaining": decision["remaining"],
            "exhausted": data["cost"].get("exhausted"),
            "stopped": data["cost"].get("stopped"),
        },
        "decide": data.get("decide"),
        "cleanup": data.get("cleanup"),
        "result": data.get("result"),
        "links": {
            "session_report": f"{data['session']}/report.md",
            "decision": f"{data['session']}/decision.json",
            "ledger": str(ledger.path),
            "logs": str(manager.log_dir),
        },
    }


def _cell(value):
    text = "—" if value is None or value == "" else str(value)
    return text.replace("|", "\\|").replace("\n", " ")[:160]


def render(content):
    lines = [f"# LSF orchestration — {content['run_id']}", ""]
    decide = content.get("decide") or {}
    cleanup = content.get("cleanup") or {}
    lines += [
        f"- Session: `{content['session']}`",
        f"- Verdict: **{decide.get('status', 'not decided')}**"
        + (f" (exit {decide['exit_code']})" if "exit_code" in decide else ""),
        f"- Cleanup: {cleanup.get('status', 'not run')}"
        + (f" — {cleanup['reason']}" if cleanup.get("reason") else ""),
    ]
    if content.get("stop_reason"):
        lines.append(f"- Stopped: {content['stop_reason']}")
    lines += [
        f"- Session report: `{content['links']['session_report']}`; decision: "
        f"`{content['links']['decision']}`",
        f"- Ledger: `{content['links']['ledger']}`; logs: `{content['links']['logs']}`",
        "",
        "## Jobs",
        "",
        "| Stage | Attempt | Job | Status | Outcome | Exit | Host | Reason | Output |",
        "| --- | ---: | --- | --- | --- | ---: | --- | --- | --- |",
    ]
    for job in content["jobs"]:
        lines.append(
            "| "
            + " | ".join(
                _cell(v)
                for v in (
                    job["stage"],
                    job["attempt"],
                    job["job_id"],
                    job["status"],
                    job["outcome"],
                    job["exit_code"],
                    job["host"],
                    job["reason"],
                    job["output"],
                )
            )
            + " |"
        )
    cost = content["cost"]
    lines += [
        "",
        "## Cost attempts",
        "",
        f"{cost['used']} of {cost['limit']} cost jobs used (one initial job and up to "
        f"{cost['limit'] - 1} noise retries); {cost['remaining']} remain.",
        "",
    ]
    if cost.get("exhausted"):
        lines += [
            f"**Retry budget exhausted:** {cost['exhausted']['reason']}. No clean cost "
            "measurement exists; this is not an observed cost regression.",
            "",
        ]
    elif cost.get("stopped"):
        lines += [f"Cost retries stopped: {cost['stopped']['reason']}.", ""]
    if cost["attempts"]:
        lines += [
            "| Attempt | Job | Outcome | Host | CPU | Phase | Foreign CPU | Involuntary/s "
            "| Evidence |",
            "| ---: | --- | --- | --- | --- | --- | ---: | ---: | --- |",
        ]
        for job in cost["attempts"]:
            fraction = job["foreign_fraction"]
            rate = job["involuntary_per_s"]
            lines.append(
                "| "
                + " | ".join(
                    _cell(v)
                    for v in (
                        job["attempt"],
                        job["job_id"],
                        job["outcome"],
                        job["host"],
                        job["cpu"],
                        job["phase"],
                        f"{fraction:.1%}" if fraction is not None else None,
                        f"{rate:.1f}" if rate is not None else None,
                        job["evidence"] or job["preflight"] or (job["logs"] or {}).get("log"),
                    )
                )
                + " |"
            )
        lines.append("")
    lines += ["## Manager invocations", ""]
    for manager in content["managers"]:
        lines.append(
            f"- {manager['index']}: job {manager.get('job_id')} on {manager.get('host')}, "
            f"started {manager['started_at']}, exit {manager.get('exit_code', '—')}, "
            f"log `{(manager.get('logs') or {}).get('log')}`"
        )
    return "\n".join(lines) + "\n"


def write(manager):
    content = build(manager)
    write_json(manager.lsf_dir / "report.json", content)
    atomic_bytes(manager.lsf_dir / "report.md", render(content).encode())
    return content
