"""Baseline-only preflight and reusable estimator-specific calibration."""

import math
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
from pathlib import Path

from qtb.canonical import digest, file_hash, read_json, write_json
from qtb.config import STAGES
from qtb.coordinator.costs import calibrate_panel, measure_panel
from qtb.coordinator.storage import read_records
from qtb.errors import Incomplete
from qtb.evaluator import evaluate_quality, record
from qtb.evaluator.statistics import sign_flip_calibration


class CalibrationIncomplete(Incomplete):
    def __init__(self, phase, detail):
        super().__init__(detail)
        self.phase = phase


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


def required_cost_panels(comparison):
    """The preset panel is measured always, but guarded for relevant or unknown scope."""
    panels = cost_panels(comparison)
    scope = comparison.run["scope"].values()
    preset_scope = any(
        set(value["stages"]) == set(STAGES) or value.get("unmapped_paths") for value in scope
    )
    changed_paths = (
        path.lower().replace("\\", "/") for path in comparison.run.get("changed_paths", [])
    )
    preset_paths = any(
        "preset_passmanager" in path or "/target" in path for path in changed_paths
    )
    if not preset_scope and not preset_paths:
        panels.pop("preset", None)
    return panels


def calibration_implementation_identity():
    """Fingerprint code that can change baseline calibration observations or rules.

    The full harness wheel also contains CLI, reporting and replay code. Changes
    there cannot alter these measurements and should not discard calibration.
    """
    qtb_root = Path(__file__).resolve().parents[1]
    sources = {"qtb/errors.py": qtb_root / "errors.py"}
    for package in ("canonical", "config", "metrics", "evaluator", "envbuild"):
        for path in (qtb_root / package).rglob("*.py"):
            sources[f"qtb/{path.relative_to(qtb_root)}"] = path
    for path in (qtb_root / "config/schemas").glob("*.json"):
        sources[f"qtb/{path.relative_to(qtb_root)}"] = path
    for name in (
        "__init__.py",
        "calibration.py",
        "costs.py",
        "process.py",
        "storage.py",
        "checks.py",
    ):
        sources[f"qtb/coordinator/{name}"] = qtb_root / "coordinator" / name
    for package in ("qtb_worker", "qtb_verifier"):
        spec = find_spec(package)
        if spec is None or not spec.submodule_search_locations:
            raise Incomplete(f"Missing calibration implementation package: {package}")
        root = Path(next(iter(spec.submodule_search_locations)))
        for path in root.rglob("*.py"):
            sources[f"{package}/{path.relative_to(root)}"] = path
    return digest({name: file_hash(path) for name, path in sorted(sources.items())})


def calibration_key(comparison):
    return digest(
        {
            "baseline": comparison.run["builds"]["baseline"]["id"],
            "manifest": comparison.run["hashes"]["manifest"],
            "policy": comparison.run["hashes"]["policy"],
            "machine": comparison.run["machine"],
            "implementation": calibration_implementation_identity(),
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
    for case in cases:
        for block in ("KB1", "KB2"):
            selected = [
                r
                for r in rows
                if r["case_id"] == case["case_id"]
                and r["revision"] == "baseline"
                and r["seed_block"] == block
            ]
            if len(selected) != case["seeds_per_block"] or any(
                any(r.get(metric) is None for metric in ("D2", "N2"))
                or not any(
                    c["oracle"] == "C0" and c["status"] == "verified" for c in r.get("checks", [])
                )
                for r in selected
            ):
                raise Incomplete(f"Incomplete baseline calibration: {case['case_id']} / {block}")
    prerequisites = [
        record(id_, "completeness", "passed") for id_ in comparison.policy["quality_prerequisites"]
    ]
    records, _, summaries = evaluate_quality(
        comparison.manifest, comparison.policy, paired, prerequisites
    )
    guard_ids = {r["id"] for r in records if r["kind"] == "guard"}
    guards = []
    for name, result in summaries.items():
        if name not in guard_ids or "deltas" not in result:
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
    result = sign_flip_calibration(
        guards,
        rng_seed=comparison.policy["rng_seed"],
        multiplier=comparison.policy["guard_multiplier"],
    )
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
    if summary and "false_rejection" not in summary:
        summary = None
    if summary and datetime.now(UTC) > datetime.fromisoformat(summary["created_at"]) + timedelta(
        days=30
    ):
        summary = None
    created = summary is None
    if created:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            for block in ("B0", "KB1", "KB2"):
                comparison.quality(cases, block=block, revisions=("baseline",))
            comparison.audit(
                cases,
                read_records(comparison.directory / "observations.jsonl"),
                revisions=("baseline",),
            )
            if not any(
                r["id"] == "audit/determinism" and r["result"] == "passed"
                for r in comparison.records
            ):
                raise Incomplete("Baseline determinism audit failed before calibration")
            quality = calibrate_quality(comparison, cases)
            quality["role_failures"] = freeze_roles(comparison, cases)
            quality["freeze_allowed"] &= not quality["role_failures"]
        except Incomplete as exc:
            raise CalibrationIncomplete("quality", str(exc)) from exc
        if quality["role_failures"]:
            comparison.evidence(
                record(
                    "baseline/preflight",
                    "correctness",
                    "failed",
                    "reference",
                    detail="Proposed deterministic or canary role failed the 300-seed audit",
                    cases=quality["role_failures"],
                )
            )
        cost = {}
        for name, (panel, estimator) in cost_panels(comparison, all_panels=True).items():
            comparison.progress(f"Calibrating {name}: two independent baseline builds.")
            try:
                cost[name] = calibrate_panel(
                    comparison.run,
                    comparison.run["builds"],
                    panel,
                    estimator,
                    directory / name,
                    comparison.fixtures,
                    comparison.policy["measurement_protocol"],
                )
            except Incomplete as exc:
                raise CalibrationIncomplete("cost", f"{name}: {exc}") from exc
        summary = {
            "id": key,
            "created_at": datetime.now(UTC).isoformat(),
            "quality": quality,
            "cost": cost,
            "hashes": comparison.run["hashes"],
            "machine": comparison.run["machine"],
        }
    guarded = required_cost_panels(comparison)
    rejections = [summary["cost"][name]["null_rejections"] for name in guarded]
    cost_rate = (
        sum(any(row) for row in zip(*rejections, strict=True)) / len(rejections[0])
        if rejections
        else 0.0
    )
    quality_rate = summary["quality"]["false_rejection_rate"]
    combined = 1 - (1 - quality_rate) * (1 - cost_rate)
    summary["false_rejection"] = {
        "quality": quality_rate,
        "cost": cost_rate,
        "combined": combined,
        "freeze_allowed": combined <= 0.1,
        "noisy_guard_count": summary["quality"]["noisy_guard_count"] + len(guarded),
        "method": "1 - (1 - p_quality) * (1 - p_cost); independent quality and cost sessions",
    }
    if created:
        write_json(summary_path, summary)
    comparison.run["calibrations"] = summary
    comparison.save()
    if summary["quality"].get("role_failures"):
        comparison.evidence(
            record(
                "baseline/preflight",
                "correctness",
                "failed",
                "reference",
                cases=summary["quality"]["role_failures"],
                detail="Frozen baseline roles failed the 300-seed audit",
            )
        )
    comparison.evidence(
        record(
            "calibration/quality",
            "completeness",
            "passed"
            if summary["quality"]["freeze_allowed"] and summary["false_rejection"]["freeze_allowed"]
            else "unresolved",
        )
    )
    comparison.evidence(
        record(
            "calibration/cost",
            "completeness",
            "passed"
            if all(summary["cost"][name]["freeze_allowed"] for name in guarded)
            else "unresolved",
        )
    )
    return summary


def measure_costs(comparison):
    calibration = comparison.run.get("calibrations")
    if calibration is None:
        raise Incomplete("No valid calibration for this run")
    guarded = required_cost_panels(comparison)
    for name, (cases, estimator) in cost_panels(comparison).items():
        comparison.progress(f"Measuring {name}: fresh interleaved baseline/control/evolved arms.")
        try:
            result = measure_panel(
                comparison.run,
                comparison.run["builds"],
                cases,
                estimator,
                comparison.directory / "cost" / name,
                comparison.fixtures,
                calibration["cost"][name],
                comparison.policy["measurement_protocol"],
                guarded=name in guarded,
            )
        except Incomplete as exc:
            if name in guarded:
                raise
            comparison.evidence(
                record(
                    f"{comparison.prefix}5/{name}",
                    "cost",
                    "not_evaluated",
                    observed_result="unresolved",
                    detail=str(exc),
                )
            )
            continue
        details = {k: v for k, v in result.items() if k != "result"}
        if name not in guarded:
            details["observed_result"] = result["result"]
        comparison.evidence(
            record(
                f"{comparison.prefix}5/{name}",
                "cost",
                result["result"] if name in guarded else "not_evaluated",
                **details,
            )
        )


def freeze_roles(comparison, cases):
    """Verify every proposed seed-blind role across all 300 baseline seeds."""
    from qtb.coordinator import structural_result
    from qtb.coordinator.storage import append_record

    path = comparison.directory / "role-freeze.jsonl"
    saved = read_records(path)
    known = {(r["case_id"], r["seed"]): r for r in saved}
    failures = []
    ordinary = read_records(comparison.directory / "observations.jsonl")
    for row in ordinary:
        if row["revision"] == "baseline" and "D2" in row and "N2" in row:
            known.setdefault((row["case_id"], row["seed"]), row)
    for case in cases:
        if case["role"] not in {"deterministic", "zero_baseline", "canary"}:
            continue
        target = read_json(comparison.fixtures / case["target"]["file"])
        missing = [seed for seed in range(300) if (case["case_id"], seed) not in known]
        batch_size = comparison.policy["measurement_protocol"]["quality_batch_size"]
        for start in range(0, len(missing), batch_size):
            for result in comparison.job(
                "baseline", case, "quality", missing[start : start + batch_size]
            ):
                row = {"case_id": case["case_id"], "seed": result["seed"]}
                if result["status"] == "ok":
                    row.update(
                        structural_result(
                            result["output"],
                            target,
                            result["layout"],
                            case["logical_qubits"],
                            constraint_form=case["constraint_form"],
                        )
                    )
                else:
                    row["status"] = "mismatch"
                append_record(path, row)
                known[case["case_id"], result["seed"]] = row
        values = {
            (
                known.get((case["case_id"], s), {}).get("D2"),
                known.get((case["case_id"], s), {}).get("N2"),
            )
            for s in range(300)
        }
        if (
            len(values) != 1
            or (None, None) in values
            or any(
                known.get((case["case_id"], s), {}).get("status") == "mismatch"
                or any(
                    c.get("oracle") == "C0" and c.get("status") != "verified"
                    for c in known.get((case["case_id"], s), {}).get("checks", [])
                )
                for s in range(300)
            )
        ):
            failures.append(case["case_id"])
        if case["role"] == "zero_baseline" and values != {(0, 0)}:
            failures.append(case["case_id"])
        if case["role"] == "canary" and values != {
            (case["expected"]["D2"], case["expected"]["N2"])
        }:
            failures.append(case["case_id"])
    return sorted(set(failures))
