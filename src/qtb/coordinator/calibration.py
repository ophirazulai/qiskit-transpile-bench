"""Baseline-only preflight and reusable estimator-specific calibration."""

import math
from datetime import UTC, datetime, timedelta

from qtb.canonical import digest, read_json, write_json
from qtb.coordinator.costs import calibrate_panel, measure_panel
from qtb.coordinator.storage import read_records
from qtb.errors import Incomplete
from qtb.evaluator import evaluate_quality, record
from qtb.evaluator.statistics import sign_flip_calibration


def cost_panels(comparison, all_panels=False):
    cases = comparison.manifest["cases"]
    panels = {
        name: (
            [c for c in cases if c.get("panel") == name],
            "memory" if name == "memory" else "timing",
        )
        for name in ("timing", "preset", "confirm-timing", "memory")
    }
    panels = {name: pair for name, pair in panels.items() if pair[0]}
    scope = {s for value in comparison.run["scope"].values() for s in value["stages"]}
    if all_panels or scope & {"layout", "routing"}:
        if comparison.run["profile"] == "confirm-profile":
            groups = {
                c["input_group"]
                for c in cases
                if c.get("panel") == "memory" and c["input_group"] != "multiplier_h18_n20"
            }
            companion = [
                next(
                    c
                    for c in cases
                    if c["input_group"] == group
                    and c["role"] == "scored"
                    and c["optimization_level"] == 2
                )
                for group in sorted(groups)
            ]
        else:
            companion = [
                c for c in cases if c.get("panel") == "timing" and c["modes"] == ["timing_reuse"]
            ]
        panels["companion"] = (companion, "companion")
    return panels


def calibration_key(comparison):
    return digest(
        {
            "baseline": comparison.run["builds"]["baseline"]["id"],
            "hashes": comparison.run["hashes"],
            "machine": comparison.run["machine"],
        }
    )


def calibrate_quality(comparison, cases):
    rows = read_records(comparison.directory / "observations.jsonl")
    paired = [
        dict(
            row,
            revision="baseline" if row["seed_block"] == "KB1" else "evolved",
            seed=row["seed"] % 100,
            seed_block="B0",
        )
        for row in rows
        if row["revision"] == "baseline" and row["seed_block"] in {"KB1", "KB2"}
    ]
    prerequisites = [
        record(id_, "completeness", "passed") for id_ in comparison.policy["quality_prerequisites"]
    ]
    _, _, summaries = evaluate_quality(
        comparison.manifest, comparison.policy, paired, prerequisites
    )
    guards = []
    for name, result in summaries.items():
        if (
            "deltas" not in result
            or name.endswith("/improvement")
            or name == "leave_iterations_out"
        ):
            continue
        guard = {"id": name, "deltas": result["deltas"]}
        if "/cap/" in name:
            guard["ln_cap"] = math.log(comparison.policy["quality_cap"])
            case_id = name.split("/cap/", 1)[1].rsplit("/", 1)[0]
            case = next(c for c in cases if c["case_id"] == case_id)
            guard["se_guard"] = case["role"] == "guard" and not case.get("panel")
        guards.append(guard)
    if not guards:
        raise Incomplete("No complete baseline calibration panels")
    result = sign_flip_calibration(guards, rng_seed=comparison.policy["rng_seed"])
    failures = []
    for case in cases:
        if case["role"] not in {"deterministic", "zero_baseline", "canary"}:
            continue
        values = {
            (r.get("D2"), r.get("N2"))
            for r in rows
            if r["case_id"] == case["case_id"] and r["revision"] == "baseline"
        }
        if len(values) != 1 or (None, None) in values:
            failures.append(case["case_id"])
        if case["role"] == "zero_baseline" and values != {(0, 0)}:
            failures.append(case["case_id"])
    result["role_failures"] = sorted(set(failures))
    result["freeze_allowed"] &= not failures
    return result


def preflight(comparison, cases, force=False):
    key = calibration_key(comparison)
    directory = comparison.root / "calibrations" / key
    summary_path = directory / "summary.json"
    summary = read_json(summary_path) if summary_path.exists() and not force else None
    if summary and datetime.now(UTC) > datetime.fromisoformat(summary["created_at"]) + timedelta(
        days=30
    ):
        summary = None
    if summary is None:
        directory.mkdir(parents=True, exist_ok=True)
        for block in ("B0", "KB1", "KB2"):
            comparison.quality(cases, block=block, revisions=("baseline",))
        quality = calibrate_quality(comparison, cases)
        quality["role_failures"] = freeze_roles(comparison, cases)
        quality["freeze_allowed"] &= not quality["role_failures"]
        if quality["role_failures"]:
            comparison.evidence(record("baseline/preflight", "correctness", "failed", "reference",
                                       detail="Proposed deterministic or canary role failed the 300-seed audit",
                                       cases=quality["role_failures"]))
        cost = {}
        for name, (panel, estimator) in cost_panels(comparison, all_panels=True).items():
            comparison.progress(f"Calibrating {name}: two independent baseline builds.")
            cost[name] = calibrate_panel(
                comparison.run,
                comparison.run["builds"],
                panel,
                estimator,
                directory / name,
                comparison.fixtures,
            )
        summary = {
            "id": key,
            "created_at": datetime.now(UTC).isoformat(),
            "quality": quality,
            "cost": cost,
            "hashes": comparison.run["hashes"],
            "machine": comparison.run["machine"],
        }
        write_json(summary_path, summary)
    comparison.run["calibrations"] = summary
    comparison.save()
    comparison.evidence(
        record(
            "calibration/quality",
            "completeness",
            "passed" if summary["quality"]["freeze_allowed"] else "unresolved",
        )
    )
    comparison.evidence(
        record(
            "calibration/cost",
            "completeness",
            "passed"
            if all(c["freeze_allowed"] for c in summary["cost"].values())
            else "unresolved",
        )
    )
    return summary


def measure_costs(comparison):
    calibration = comparison.run.get("calibrations")
    if calibration is None:
        raise Incomplete("No valid calibration for this run")
    for name, (cases, estimator) in cost_panels(comparison).items():
        comparison.progress(f"Measuring {name}: fresh interleaved baseline/control/evolved arms.")
        result = measure_panel(
            comparison.run,
            comparison.run["builds"],
            cases,
            estimator,
            comparison.directory / "cost" / name,
            comparison.fixtures,
            calibration["cost"][name],
        )
        comparison.evidence(
            record(
                f"{comparison.prefix}5/{name}",
                "cost",
                result["result"],
                **{k: v for k, v in result.items() if k != "result"},
            )
        )


def freeze_roles(comparison, cases):
    """Verify every proposed seed-blind role across all 300 baseline seeds."""
    from qtb.coordinator import structural_result
    from qtb.coordinator.storage import append_record

    path = comparison.directory/'role-freeze.jsonl'
    saved = read_records(path)
    known = {(r['case_id'],r['seed']): r for r in saved}
    failures = []
    ordinary = read_records(comparison.directory/'observations.jsonl')
    for row in ordinary:
        if row['revision'] == 'baseline' and 'D2' in row and 'N2' in row:
            known.setdefault((row['case_id'],row['seed']),row)
    for case in cases:
        if case['role'] not in {'deterministic','zero_baseline','canary'}:
            continue
        target=read_json(comparison.fixtures/case['target']['file'])
        missing=[seed for seed in range(300) if (case['case_id'],seed) not in known]
        for start in range(0,len(missing),25):
            for result in comparison.job('baseline',case,'quality',missing[start:start+25]):
                row={'case_id':case['case_id'],'seed':result['seed']}
                if result['status']=='ok':
                    row.update(structural_result(result['output'],target,result['layout'],case['logical_qubits']))
                else:
                    row['status']='mismatch'
                append_record(path,row);known[case['case_id'],result['seed']]=row
        values={(known.get((case['case_id'],s),{}).get('D2'),
                 known.get((case['case_id'],s),{}).get('N2')) for s in range(300)}
        if len(values)!=1 or (None,None) in values:
            failures.append(case['case_id'])
        if case['role']=='zero_baseline' and values!={(0,0)}:
            failures.append(case['case_id'])
        if case['role']=='canary' and values!={(case['expected']['D2'],case['expected']['N2'])}:
            failures.append(case['case_id'])
    return sorted(set(failures))
