"""Paired quality estimators and frozen hierarchical weights."""

import math
import random
from collections import defaultdict
from statistics import fmean, stdev

from qtb.errors import HarnessError, Incomplete

WEIGHT_DIMENSIONS = (
    "family",
    "optimization_level",
    "size_band",
    "topology",
    "native_basis",
    "input_group",
    "variant",
)


def hierarchical_weights(cases, dimensions=WEIGHT_DIMENSIONS):
    if not cases:
        raise HarnessError("Cannot weight an empty panel")
    result = {}

    def divide(rows, depth, weight):
        if depth == len(dimensions):
            for row in rows:
                result[row["case_id"]] = weight / len(rows)
            return
        children = defaultdict(list)
        for row in rows:
            children[row.get(dimensions[depth], "numeric")].append(row)
        for child in children.values():
            divide(child, depth + 1, weight / len(children))

    divide(cases, 0, 1.0)
    if len(result) != len(cases):
        raise HarnessError("Duplicate case IDs")
    return result


def estimate(deltas):
    if len(deltas) < 2 or not all(math.isfinite(x) for x in deltas):
        raise Incomplete("At least two finite paired observations are required")
    mean = fmean(deltas)
    se = stdev(deltas) / math.sqrt(len(deltas))
    return {
        "ln_score": mean,
        "score": math.exp(mean),
        "SE": se,
        "ln_score_plus_2SE": mean + 2 * se,
        "deltas": list(deltas),
    }


def paired_panel(cases, observations, metric, seeds, weights=None):
    """observations[(case_id, revision, seed)] contains harness-owned metrics."""
    if not cases or len(seeds) < 2 or len(set(seeds)) != len(seeds):
        raise Incomplete("Empty panel or invalid seed block")
    weights = weights or {c["case_id"]: c["weight"] for c in cases}
    total = sum(weights[c["case_id"]] for c in cases)
    if total <= 0 or any(weights[c["case_id"]] <= 0 for c in cases):
        raise HarnessError("Positive panel weights required")
    deltas = [0.0] * len(seeds)
    per_case = {}
    for case in cases:
        case_id = case["case_id"]
        differences = []
        for seed in seeds:
            try:
                ref = observations[case_id, "baseline", seed][metric]
                candidate = observations[case_id, "evolved", seed][metric]
            except KeyError as exc:
                raise Incomplete(f"Missing {metric}: {case_id}, seed {seed}") from exc
            if (
                not isinstance(ref, (int, float))
                or not isinstance(candidate, (int, float))
                or not math.isfinite(ref)
                or not math.isfinite(candidate)
                or min(ref, candidate) <= 0
            ):
                raise Incomplete(f"Non-positive or invalid {metric}: {case_id}, seed {seed}")
            differences.append(math.log(candidate) - math.log(ref))
        per_case[case_id] = estimate(differences)
        per_case[case_id]["worst_seed"] = seeds[max(range(len(seeds)), key=differences.__getitem__)]
        for i, difference in enumerate(differences):
            deltas[i] += weights[case_id] / total * difference
    return {
        **estimate(deltas),
        "metric": metric,
        "cases": per_case,
        "seeds": list(seeds),
        "reference": "baseline",
    }


def quantile(values, p):
    if not values or not 0 <= p <= 1:
        raise HarnessError("Invalid quantile")
    values = sorted(values)
    position = (len(values) - 1) * p
    lo = int(position)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (position - lo) * (values[hi] - values[lo])


def cluster_bootstrap(cases, panel, replicates=10000, rng_seed=20260924):
    """Report-only family-stratified group bootstrap with shared seed draws.

    Resample group weight shares; center on the frozen-weight estimator so unequal
    configuration support never changes the original family's weight.
    """
    rng = random.Random(rng_seed)
    families = defaultdict(lambda: defaultdict(list))
    for case in cases:
        families[case["family"]][case["input_group"]].append(case)
    counts = {family: len(groups) for family, groups in families.items()}
    if any(n < 3 for n in counts.values()):
        return {
            "status": "unverified",
            "reason": "Fewer than three groups per family",
            "group_counts": counts,
            "report_only": True,
        }
    nseeds = len(panel["deltas"])
    family_data = []
    for groups in families.values():
        width = len(groups)
        family_weight = sum(c["weight"] for rows in groups.values() for c in rows)
        # Mean over these vectors equals the family's frozen-weight effect.
        vectors = [
            [
                sum(c["weight"] * panel["cases"][c["case_id"]]["deltas"][s] for c in rows)
                * width
                / family_weight
                for s in range(nseeds)
            ]
            for rows in groups.values()
        ]
        family_data.append((family_weight, vectors))
    samples = []
    for _ in range(replicates):
        seeds = rng.choices(range(nseeds), k=nseeds)
        value = 0.0
        for weight, vectors in family_data:
            effects = [fmean(vector[s] for s in seeds) for vector in vectors]
            n = len(effects)
            center = fmean(effects)
            draw = fmean(rng.choices(effects, k=n))
            value += weight * (center + math.sqrt(n / (n - 1)) * (draw - center))
        samples.append(value)
    return {
        "status": "reported",
        "report_only": True,
        "U_instance": quantile(samples, 0.95),
        "rng_seed": rng_seed,
        "replicates": replicates,
        "group_counts": counts,
    }


def sign_flip_calibration(guards, replicates=10000, rng_seed=20260924, multiplier=3.0):
    """Each guard has seed deltas and optional log cap; flip entire seed rows."""
    if not guards or any(len(g["deltas"]) < 2 for g in guards):
        raise HarnessError("Calibration requires a complete guard set")
    n = max(len(g["deltas"]) for g in guards)
    rng, failures = random.Random(rng_seed), 0
    for _ in range(replicates):
        signs = [rng.choice((-1, 1)) for _ in range(n)]
        failed = False
        for guard in guards:
            result = estimate(
                [s * d for s, d in zip(signs[: len(guard["deltas"])], guard["deltas"], strict=True)]
            )
            if (
                guard.get("se_guard", True) and result["ln_score"] > multiplier * result["SE"]
            ) or result["ln_score"] > guard.get("ln_cap", math.inf):
                failed = True
        failures += failed
    rate = failures / replicates
    return {
        "false_rejection_rate": rate,
        "monte_carlo_SE": math.sqrt(rate * (1 - rate) / replicates),
        "replicates": replicates,
        "rng_seed": rng_seed,
        "noisy_guard_count": sum(
            g.get("se_guard", True) and estimate(g["deltas"])["SE"] > 0 for g in guards
        ),
        "freeze_allowed": rate <= 0.1,
    }
