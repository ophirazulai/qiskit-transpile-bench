"""Decision and report artifacts, without any Qiskit import."""

from pathlib import Path

from qtb.canonical import atomic_bytes, write_json
from qtb.config import validate
from qtb.evaluator import verdict


def make_decision(run, records, required_ids, summaries, observations=()):
    status = verdict(records, required_ids)
    fingerprints = {}
    for row in observations:
        if row.get("seed_block") == "B0" and row.get("seed") == 0 and row.get("fingerprint"):
            fingerprints.setdefault(row["case_id"], {})[row["revision"]] = row["fingerprint"]
    decision = {
        "format": "qtb-decision/1",
        "status": status,
        "improved_under_constraints": {
            "PASS": True,
            "NO_IMPROVEMENT": False,
            "CONSTRAINT_VIOLATION": False,
        }.get(status),
        "profile": run["profile"],
        "hashes": run["hashes"],
        "seed_block": "B0",
        "decisions_before": run.get("decisions_before", 0),
        "run_id": run["run_id"],
        "identities": {k: v["id"] for k, v in run.get("builds", {}).items()},
        "constraints": records,
        "required_ids": sorted(required_ids),
        "summaries": summaries,
        "scope": run.get("scope", {}),
        "measurement_timestamp": run.get("created_at"),
        "calibration": run.get("calibrations", {}).get("false_rejection"),
        "fingerprint_changes": {
            case: pair
            for case, pair in sorted(fingerprints.items())
            if pair.get("baseline") != pair.get("evolved")
        },
        "coverage_gaps": run.get("coverage_gaps", []),
        "reasons": [
            {"code": r["id"], "result": r["result"], "detail": r.get("detail", "")}
            for r in records
            if r["result"] not in {"passed", "passed_on_rerun"}
        ],
        "missing_records": sorted(set(required_ids) - {r["id"] for r in records}),
    }
    validate("decision", decision)
    return decision


def render_report(decision):
    lines = [
        f"# {decision['status']} — {decision['profile']}",
        "",
        f"Earlier decisions for this manifest: {decision['decisions_before']}.",
        "",
        "The baseline named for this comparison is the reference for every test.",
        "Iterate on iterations-profile; use confirm-profile as a check, not a tuning loop.",
        "This verdict concerns the fixed workload and its declared semantic contracts.",
        "",
    ]
    calibration = decision.get("calibration")
    if calibration:
        lines += [
            f"Noisy guards: {calibration['noisy_guard_count']}. Calibrated family-wise "
            f"false-rejection estimate: {calibration['combined']:.2%} "
            f"(quality {calibration['quality']:.2%}; cost {calibration['cost']:.2%}).",
            "",
        ]
    if decision["reasons"] or decision["missing_records"]:
        lines += ["## Constraints needing attention", ""]
        for reason in decision["reasons"]:
            lines.append(f"- **{reason['result']}** `{reason['code']}`: {reason['detail']}")
        for missing in decision["missing_records"]:
            lines.append(f"- **missing** `{missing}`")
        lines.append("")
    lines += [
        "## Quality estimates",
        "",
        "| Panel | Ratio | Paired SE (log) | Upper 2 SE |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, summary in decision["summaries"].items():
        if "score" in summary and "/cap/" not in name:
            lines.append(
                f"| {name} | {summary['score']:.6f} | {summary['SE']:.6f} | "
                f"{summary['ln_score_plus_2SE']:.6f} |"
            )
    bootstrap = decision["summaries"].get("instance_bootstrap")
    if bootstrap:
        lines += [
            "",
            "Instance bootstrap (report-only): "
            + (
                f"95% upper log effect {bootstrap['U_instance']:.6f}."
                if bootstrap.get("status") == "reported"
                else bootstrap.get("reason", "Unavailable")
            ),
            "",
        ]
    lines += [
        "",
        "## Per-case changes",
        "",
        "| Case / metric | Ratio | Worst seed |",
        "| --- | ---: | ---: |",
    ]
    for name, summary in decision["summaries"].items():
        if "/cap/" in name:
            item = next(iter(summary["cases"].values()))
            lines.append(
                f"| {name.split('/cap/', 1)[1]} | {summary['score']:.6f} | {item['worst_seed']} |"
            )
    lines += [
        "",
        "## Compilation cost",
        "",
        "| Panel | Result | Candidate log ratio | Control log ratio |",
        "| --- | --- | ---: | ---: |",
    ]
    for row in decision["constraints"]:
        if row["kind"] == "cost" and "candidate" in row:
            lines.append(
                f"| {row['id']} | {row['result']} | {row['candidate']['ln_panel']:.6f} | "
                f"{row['control']['ln_panel']:.6f} |"
            )
    lines += [
        "",
        "## Coverage and provenance",
        "",
        "Automatic acceptance requires verified coverage of all changed stages "
        "on every scored case.",
        "Routing replay covers layout and routing; substituted synthesis does not "
        "cover synthesis changes.",
        "Zero-input checks are not all-input equivalence claims.",
        "",
        "```json",
        __import__("json").dumps(decision.get("scope", {}), indent=2),
        "```",
        "",
    ]
    for gap in decision.get("coverage_gaps", []):
        lines.append(f"- Declared workload gap: {gap}")
    changes = decision.get("fingerprint_changes", {})
    if changes:
        lines += [
            "",
            "Pipeline fingerprints changed for: " + ", ".join(f"`{c}`" for c in changes) + ".",
            "Pass names and observed search budgets are archived in `decision.json`; "
            "unavailable budget fields remain `unknown`.",
            "",
        ]
    return "\n".join(lines)


def write_report(directory, decision):
    directory = Path(directory)
    write_json(directory / "decision.json", decision)
    atomic_bytes(directory / "report.md", render_report(decision).encode())
