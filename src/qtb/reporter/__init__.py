"""Decision and report artifacts, without any Qiskit import."""

import math
from pathlib import Path

from qtb.canonical import atomic_bytes, write_json
from qtb.config import validate
from qtb.evaluator import verdict


def _zero_baseline_deltas(manifest, observations):
    if manifest is None:
        return {
            "status": "unavailable", "reason": "Archived manifest was not supplied", "cases": []
        }
    cases = [case for case in manifest["cases"] if case["role"] == "zero_baseline"]
    if not cases:
        return {"status": "unavailable", "reason": "No zero-baseline cases", "cases": []}
    rows = {
        (row["case_id"], row["revision"], row["seed"]): row
        for row in observations
        if row.get("seed_block") == "B0" and row.get("mode") == "quality"
    }
    results = []
    for case in cases:
        for metric in ("D2", "N2"):
            deltas = []
            for seed in range(case["seeds_per_block"]):
                baseline = rows.get((case["case_id"], "baseline", seed), {}).get(metric)
                evolved = rows.get((case["case_id"], "evolved", seed), {}).get(metric)
                if not all(
                    isinstance(value, (int, float)) and math.isfinite(value)
                    for value in (baseline, evolved)
                ):
                    break
                deltas.append((evolved - baseline, seed, baseline, evolved))
            if len(deltas) != case["seeds_per_block"]:
                results.append(
                    {"case_id": case["case_id"], "metric": metric, "status": "unavailable",
                     "reason": "Incomplete paired observations"}
                )
                continue
            delta, seed, baseline, evolved = max(deltas, key=lambda item: item[0])
            results.append(
                {"case_id": case["case_id"], "metric": metric, "status": "reported",
                 "worst_delta": delta, "worst_seed": seed, "baseline": baseline,
                 "evolved": evolved, "seed_count": len(deltas)}
            )
    return {"status": "reported", "cases": results}


def make_decision(
    run,
    records,
    required_ids,
    summaries,
    observations=(),
    manifest=None,
    policy=None,
    session=None,
):
    """The verdict and its evidence. ``session`` is ``decide``'s view of the stages.

    It may carry ``status``: a verdict that the stage states impose over the evidence (a
    required stage that has not finished gives ``INCONCLUSIVE``), plus ``stages``, the
    ``stage_state_hashes`` read and report ``notes``.
    """
    observations = list(observations)
    session = session or {}
    status = verdict(records, required_ids)
    if session.get("status") and status != "ERROR":
        status = session["status"]
    prefix = "CA" if run["profile"] == "confirm-profile" else "IA"
    depth = summaries.get(f"{prefix}2/improvement", {})
    gates = summaries.get(f"{prefix}3/primary/N2", {})
    objective = (
        [{"block": "B0", "metric": "D2", "panel": "confirm" if prefix == "CA" else "cz",
          "reference": "baseline", **{key: depth[key] for key in
          ("score", "ln_score", "SE", "ln_score_plus_2SE")}}]
        if all(key in depth for key in ("score", "ln_score", "SE", "ln_score_plus_2SE"))
        else []
    )
    trade_score = (
        math.sqrt(depth["score"] * gates["score"])
        if all(isinstance(item.get("score"), (int, float)) and item["score"] > 0
               for item in (depth, gates))
        else None
    )
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
        "run_id": run["run_id"],
        "stages": session.get("stages", []),
        "stage_state_hashes": session.get("stage_state_hashes", {}),
        "notes": session.get("notes", []),
        "identities": {k: v["id"] for k, v in run.get("builds", {}).items()},
        "constraints": records,
        "required_ids": sorted(required_ids),
        "objective": objective,
        "trade_score": trade_score,
        "zero_baseline_deltas": _zero_baseline_deltas(manifest, observations),
        "summaries": summaries,
        "scope": run.get("scope", {}),
        "measurement_timestamp": run.get("created_at"),
        "cost_thresholds": (policy or {}).get("cost_thresholds"),
        "fingerprint_changes": {
            case: pair
            for case, pair in sorted(fingerprints.items())
            if pair.get("baseline") != pair.get("evolved")
        },
        "coverage_gaps": run.get("coverage_gaps", []),
        "exclusions": run.get(
            "exclusions",
            {"status": "unavailable", "reason": "No frozen exclusions artifact was recorded"},
        ),
        "reasons": [
            {"code": r["id"], "result": r["result"], "detail": r.get("detail", "")}
            for r in records
            if r["result"] not in {"passed", "passed_on_rerun"}
        ],
        "missing_records": sorted(set(required_ids) - {r["id"] for r in records}),
    }
    validate("decision", decision)
    return decision


def _duration(seconds):
    from qtb.coordinator.runlog import duration

    return duration(seconds)


def render_report(decision):
    lines = [f"# {decision['status']} — {decision['profile']}", ""]
    if decision.get("stages"):
        lines += [
            "## Stages",
            "",
            "| Stage | State | Host | Duration | Note |",
            "| --- | --- | --- | ---: | --- |",
        ]
        for row in decision["stages"]:
            seconds = row.get("seconds")
            shown = _duration(seconds) if isinstance(seconds, (int, float)) else "—"
            lines.append(
                f"| {row['stage']} | {row['status']} | {row.get('host') or '—'} | {shown} | "
                f"{row.get('note') or ''} |"
            )
        lines.append("")
    if decision.get("notes"):
        lines += [f"- {note}" for note in decision["notes"]] + [""]
    lines += [
        "The baseline named for this comparison is the reference for every test.",
        "Iterate on iterations-profile; use confirm-profile as a check, not a tuning loop.",
        "This verdict concerns the fixed workload and its declared semantic contracts.",
        "",
    ]
    fixed = decision.get("cost_thresholds")
    if fixed:
        lines += [
            "Cost thresholds are fixed by policy, not calibrated on this machine: "
            f"panel ≤ {fixed['panel_ratio']:.2f}×, a case counts only above "
            f"{fixed['case_ratio']:.2f}× and {fixed['case_floor_ns'] / 1e6:.0f} ms "
            f"({fixed['case_floor_bytes'] / 2**20:.0f} MiB for memory); the screen must "
            f"clear {fixed['screen_fraction']:.0%} of the panel band.",
            "",
        ]
    if decision["reasons"] or decision["missing_records"]:
        lines += ["## Constraints needing attention", ""]
        for reason in decision["reasons"]:
            lines.append(f"- **{reason['result']}** `{reason['code']}`: {reason['detail']}")
        for missing in decision["missing_records"]:
            lines.append(f"- **missing** `{missing}`")
        lines.append("")
    if decision.get("objective"):
        objective = decision["objective"][0]
        lines += [
            f"Primary D2 ratio: {objective['score']:.6f} "
            f"(paired SE {objective['SE']:.6f}; upper 2 SE log effect "
            f"{objective['ln_score_plus_2SE']:.6f}).",
            "",
        ]
    else:
        lines += ["Primary D2 estimate: unavailable from saved observations.", ""]
    trade = decision.get("trade_score")
    trade_text = (
        f"{trade:.6f}." if trade is not None else "unavailable; both primary ratios are required."
    )
    lines += [
        "Review trade score sqrt(D2 ratio × N2 ratio): " + trade_text,
        "",
    ]
    lines += [
        "## Quality estimates",
        "",
        "| Panel | Ratio | Paired SE (log) | Upper 2 SE |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, summary in decision["summaries"].items():
        if "score" in summary and name.startswith(("IA2/", "IA3/", "CA2/", "CA3/")):
            lines.append(
                f"| {name} | {summary['score']:.6f} | {summary['SE']:.6f} | "
                f"{summary['ln_score_plus_2SE']:.6f} |"
            )
    lines += [
        "",
        "## Marginal quality estimates",
        "",
        "| Dimension | Value | Metric | Ratio | Paired SE | Cases | Seeds |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for name, summary in sorted(decision["summaries"].items()):
        parts = name.split("/")
        if len(parts) != 4 or parts[1] not in {
            "family", "optimization_level", "size_band", "topology", "native_basis"
        }:
            continue
        dimension, value, metric = parts[1:]
        if "score" in summary:
            lines.append(
                f"| {dimension} | {value} | {metric} | {summary['score']:.6f} | "
                f"{summary['SE']:.6f} | {len(summary.get('cases', {}))} | "
                f"{len(summary.get('seeds', []))} |"
            )
        else:
            lines.append(f"| {dimension} | {value} | {metric} | unavailable | — | — | — |")
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
            "Input groups by family: "
            + (
                ", ".join(
                    f"{family}: {count}"
                    for family, count in sorted(bootstrap.get("group_counts", {}).items())
                )
                or "unavailable"
            )
            + ".",
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
        "## Zero-baseline changes",
        "",
        "| Case | Metric | Worst delta | Seed | Baseline | Evolved |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    zero = decision.get("zero_baseline_deltas", {})
    for row in zero.get("cases", []):
        if row["status"] == "reported":
            lines.append(
                f"| {row['case_id']} | {row['metric']} | {row['worst_delta']} | "
                f"{row['worst_seed']} | {row['baseline']} | {row['evolved']} |"
            )
        else:
            lines.append(f"| {row['case_id']} | {row['metric']} | unavailable | — | — | — |")
    if zero.get("status") != "reported":
        lines.append(f"Unavailable: {zero.get('reason', 'No paired observations')}.")
    lines += [
        "",
        "## Compilation cost",
        "",
        "| Panel | Result | Regime | Candidate log ratio |",
        "| --- | --- | --- | ---: |",
    ]
    for row in decision["constraints"]:
        if row["kind"] == "cost" and "candidate" in row:
            regime = row.get("regime", "normal")
            if "count" in row:
                regime += f" ({row['count']} rounds)"
            lines.append(
                f"| {row['id']} | {row['result']} | {regime} | "
                f"{row['candidate']['ln_panel']:.6f} |"
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
    coverage = next(
        (row for row in decision["constraints"] if row["id"].endswith("/stage-coverage")),
        None,
    )
    if coverage:
        lines.append(f"Stage coverage: {coverage['result']}.")
        if coverage.get("cases"):
            lines.append("Uncovered scored cases: " + ", ".join(coverage["cases"]) + ".")
    exclusions = decision.get("exclusions", {})
    lines += ["", "## Exclusions", ""]
    if isinstance(exclusions, dict) and exclusions.get("status") == "unavailable":
        lines.append("Unavailable: " + exclusions["reason"] + ".")
    else:
        lines.append(__import__("json").dumps(exclusions, sort_keys=True))
    changes = decision.get("fingerprint_changes", {})
    if changes:
        lines += [
            "",
            "## Pipeline fingerprint differences",
            "",
            "| Case | Stage | Baseline passes | Evolved passes | Budget changed |",
            "| --- | --- | --- | --- | --- |",
        ]
        for case, pair in sorted(changes.items()):
            baseline, evolved = pair.get("baseline", {}), pair.get("evolved", {})
            for stage in sorted(baseline.keys() | evolved.keys()):
                before, after = baseline.get(stage, []), evolved.get(stage, [])
                if before == after:
                    continue
                before_names = ", ".join(item["pass"] for item in before) or "—"
                after_names = ", ".join(item["pass"] for item in after) or "—"
                budget_changed = [item.get("budget") for item in before] != [
                    item.get("budget") for item in after
                ]
                lines.append(
                    f"| {case} | {stage} | {before_names} | {after_names} | "
                    f"{'yes' if budget_changed else 'no'} |"
                )
        lines += [
            "Unknown budget values remain marked `unknown` in `decision.json`.",
            "",
        ]
    return "\n".join(lines)


def write_report(directory, decision):
    directory = Path(directory)
    write_json(directory / "decision.json", decision)
    atomic_bytes(directory / "report.md", render_report(decision).encode())
