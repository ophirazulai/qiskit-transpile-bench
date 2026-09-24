"""Fail-closed verdicts and observation-only evaluation."""

import math

from qtb.errors import HarnessError, Incomplete
from qtb.evaluator.statistics import cluster_bootstrap, paired_panel

EXIT_CODES = {
    "PASS": 0,
    "NO_IMPROVEMENT": 10,
    "CONSTRAINT_VIOLATION": 20,
    "INCONCLUSIVE": 30,
    "ERROR": 40,
}
KINDS = {"harness", "correctness", "guard", "cost", "improvement", "completeness"}
RESULTS = {"passed", "failed", "passed_on_rerun", "unresolved", "not_evaluated"}


def record(id_, kind, result, subject="evolved", **details):
    return {
        "id": id_,
        "kind": kind,
        "result": result,
        "subject": subject,
        "reference": "baseline",
        **details,
    }


def verdict(records, required_ids):
    required = set(required_ids)
    if not required or len(required) != len(required_ids):
        raise HarnessError("Required record IDs must be nonempty and unique")
    by_id = {r["id"]: r for r in records}
    if len(by_id) != len(records):
        raise HarnessError("Duplicate constraint record ID")
    # Missing improvement evidence remains inconclusive. The required set must name it.
    if not any(i.endswith("/improvement") for i in required):
        raise HarnessError("Required IDs must include an improvement record")
    if any(
        i.endswith("/improvement") and i in by_id and by_id[i]["kind"] != "improvement"
        for i in required
    ):
        raise HarnessError("An improvement ID must contain an improvement record")
    for row in records:
        if (
            row["kind"] not in KINDS
            or row["result"] not in RESULTS
            or row["subject"] not in {"evolved", "reference"}
        ):
            raise HarnessError("Invalid constraint record")
        if row["result"] == "passed_on_rerun" and row["kind"] != "cost":
            raise HarnessError("Only cost checks may pass on rerun")
    failed = [r for r in records if r["result"] == "failed"]
    if any(r["kind"] == "harness" for r in failed):
        return "ERROR"
    if any(r["subject"] == "evolved" and r["kind"] != "improvement" for r in failed):
        return "CONSTRAINT_VIOLATION"
    if any(r["subject"] == "reference" for r in failed):
        return "INCONCLUSIVE"
    if any(r["kind"] == "improvement" for r in failed):
        return "NO_IMPROVEMENT"
    if all(i in by_id and by_id[i]["result"] in {"passed", "passed_on_rerun"} for i in required):
        return "PASS"
    return "INCONCLUSIVE"


def evaluate_quality(manifest, policy, rows, evidence=()):
    """Grade complete panels. Independent evidence validates the measurement protocol."""
    cases, records, summaries = manifest["cases"], list(evidence), {}
    observations = {}
    for row in rows:
        if row.get("mode") != "quality" or row.get("seed_block") != "B0":
            continue
        key = (row["case_id"], row["revision"], row["seed"])
        if key in observations:
            raise HarnessError(f"Duplicate observation: {key}")
        observations[key] = row
    confirm = manifest["profile"] == "confirm-profile"
    prefix = "CA" if confirm else "IA"
    required = set(policy["required_ids"])
    positive = [c for c in cases if c["role"] in {"scored", "guard"}]
    scored = [c for c in cases if c["role"] == "scored"]
    by_evidence = {r["id"]: r for r in evidence}
    panel_valid = all(
        by_evidence.get(i, {}).get("result") == "passed" for i in policy["quality_prerequisites"]
    )

    def panel_record(id_, subset, metric, improvement=False, weights=None):
        required.add(id_)
        kind = "improvement" if improvement else "guard"
        try:
            if not panel_valid:
                raise Incomplete("Round-trip or determinism audit evidence is missing")
            seeds = list(range(subset[0]["seeds_per_block"]))
            if any(c["seeds_per_block"] != len(seeds) for c in subset):
                raise Incomplete("Mixed seed counts inside a panel")
            result = paired_panel(subset, observations, metric, seeds, weights)
            limit = math.log(policy["practical_ratio"]) if improvement else 0
            ok = (
                result["ln_score"] + policy["improvement_multiplier"] * result["SE"] < limit
                if improvement
                else result["ln_score"] <= policy["guard_multiplier"] * result["SE"]
            )
            records.append(
                record(
                    id_,
                    kind,
                    "passed" if ok else "failed",
                    value=result["ln_score"],
                    SE=result["SE"],
                    threshold=limit,
                )
            )
            summaries[id_] = result
            return result
        except (Incomplete, IndexError) as exc:
            records.append(record(id_, kind, "unresolved", detail=str(exc)))
            return None

    objective = panel_record(f"{prefix}2/improvement", scored, "D2", True)
    panel_record(f"{prefix}3/primary/N2", scored, "N2")
    if not confirm:
        panel_record("IA3/primary/D2", scored, "D2")
    for basis in ("cx", "ecr"):
        subset = [c for c in cases if c.get("panel") == f"basis-{basis}"]
        if subset:
            for metric in ("D2", "N2"):
                panel_record(
                    f"{prefix}3/{basis}/{metric}",
                    subset,
                    metric,
                    weights={c["case_id"]: 1 / len(subset) for c in subset},
                )
    for case in positive:
        for metric in ("D2", "N2"):
            id_ = f"{prefix}4/cap/{case['case_id']}/{metric}"
            required.add(id_)
            try:
                if not panel_valid:
                    raise Incomplete("Round-trip or determinism audit evidence is missing")
                result = paired_panel(
                    [case],
                    observations,
                    metric,
                    list(range(case["seeds_per_block"])),
                    {case["case_id"]: 1},
                )
                ok = result["score"] <= policy["quality_cap"]
                if case["role"] == "guard" and not case.get("panel"):
                    ok &= result["ln_score"] <= policy["guard_multiplier"] * result["SE"]
                records.append(
                    record(
                        id_,
                        "guard",
                        "passed" if ok else "failed",
                        value=result["score"],
                        SE=result["SE"],
                    )
                )
                summaries[id_] = result
            except Incomplete as exc:
                records.append(record(id_, "guard", "unresolved", detail=str(exc)))
    for case in cases:
        if case["role"] not in {"canary", "deterministic", "zero_baseline"}:
            continue
        id_ = f"{prefix}4/exact/{case['case_id']}"
        required.add(id_)
        status, detail = "passed", ""
        try:
            if not panel_valid:
                raise Incomplete("Round-trip or determinism audit evidence is missing")
            for seed in range(case["seeds_per_block"]):
                for metric in ("D2", "N2"):
                    a = observations[case["case_id"], "baseline", seed][metric]
                    b = observations[case["case_id"], "evolved", seed][metric]
                    if case["role"] == "canary":
                        expected = case["expected"][metric]
                        if a != expected:
                            records.append(
                                record(
                                    id_ + f"/reference/{seed}/{metric}",
                                    "guard",
                                    "failed",
                                    "reference",
                                    value=a,
                                )
                            )
                        if b != expected:
                            status = "unresolved"
                    elif b > a:
                        status = "failed"
                    if case["role"] == "zero_baseline" and a != 0:
                        records.append(
                            record(
                                id_ + f"/reference/{seed}/{metric}",
                                "guard",
                                "failed",
                                "reference",
                                value=a,
                            )
                        )
        except (KeyError, Incomplete) as exc:
            if status != "failed":
                status = "unresolved"
            detail = (
                str(exc)
                if isinstance(exc, Incomplete)
                else "Missing exact-guard observation or frozen expected value"
            )
        records.append(record(id_, "guard", status, detail=detail))
    if confirm and objective:
        family_scores = []
        leave_out = []

        def report_summary(name, subset, metric):
            try:
                result = paired_panel(subset, observations, metric, list(range(100)))
            except (Incomplete, HarnessError) as exc:
                summaries[name] = {"status": "unavailable", "reason": str(exc)}
                return None
            summaries[name] = result
            return result

        for dimension in ("family", "optimization_level", "size_band", "topology", "native_basis"):
            for value in sorted({c[dimension] for c in scored}, key=str):
                subset = [c for c in scored if c[dimension] == value]
                for metric in ("D2", "N2"):
                    id_ = f"CA4/{dimension}/{value}/{metric}"
                    if dimension in {"family", "optimization_level"}:
                        result = panel_record(id_, subset, metric)
                    else:
                        result = report_summary(id_, subset, metric)
                    if dimension == "family" and metric == "D2":
                        family_scores.append(result)
                if dimension == "family":
                    rest = [c for c in scored if c[dimension] != value]
                    leave_out.append(
                        report_summary(f"leave_family_out/{value}", rest, "D2")
                    )
        breadth_complete = (
            len(family_scores) == 8
            and all(r is not None for r in family_scores)
            and all(r is not None for r in leave_out)
        )
        breadth = breadth_complete and (
            sum(r["ln_score_plus_2SE"] < 0 for r in family_scores) >= 4
            and all(r["score"] < 1 for r in leave_out)
        )
        records.append(
            record(
                "CA3/breadth",
                "improvement",
                "passed" if breadth else "failed" if breadth_complete else "unresolved",
                detail=(
                    "" if breadth_complete else "Family or leave-one-family-out summary unavailable"
                ),
            )
        )
        required.add("CA3/breadth")
        summaries["instance_bootstrap"] = cluster_bootstrap(
            scored, objective, policy["bootstrap_replicates"], policy["rng_seed"]
        )
        rest = [c for c in scored if c["input_group"] not in policy["iterations_groups"]]
        report_summary("leave_iterations_out", rest, "D2")
    elif confirm:
        required.add("CA3/breadth")
    return records, sorted(required), summaries
