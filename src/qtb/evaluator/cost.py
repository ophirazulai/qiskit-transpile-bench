"""Two-arm cost guards against thresholds fixed by policy."""

import math
from statistics import fmean, median

from qtb.canonical import digest
from qtb.errors import HarnessError, Incomplete
from qtb.extensions import check_cost_evidence

# Fallback (screen, normal, rerun) counts when a policy names none.
COUNTS = {"timing": (0, 10, 20), "companion": (0, 3, 6), "memory": (0, 5, 10)}
MEASURED_ARMS = ("baseline", "evolved")


def regime_counts(estimator, protocol=None):
    """Per-regime sample counts for one estimator, from the measurement protocol.

    A ``screen`` count of zero (or a missing key) disables the short early-stop
    regime. ``normal`` is the full measurement; ``rerun`` is the fresh doubled
    measurement after a breach.
    """
    if estimator not in COUNTS:
        raise HarnessError(f"Unknown cost estimator: {estimator}")
    screen, normal, rerun = COUNTS[estimator]
    protocol = protocol or {}
    multiplier = protocol.get("rerun_multiplier", 2)
    if estimator == "timing":
        normal = protocol.get("timing_rounds", normal)
        screen = protocol.get("screen_rounds", screen)
    elif estimator == "companion":
        normal = protocol.get("companion_rounds", normal)
    else:
        normal = protocol.get("memory_processes", normal)
    rerun = normal * multiplier
    if not 0 <= screen < normal < rerun:
        raise HarnessError(f"Invalid {estimator} regime counts: {(screen, normal, rerun)}")
    regimes = {"normal": normal, "rerun": rerun}
    if screen:
        regimes = {"screen": screen, **regimes}
    return regimes


def thresholds_id(policy):
    """Identity of the frozen thresholds a bundle was judged against."""
    return digest(policy["cost_thresholds"])


def thresholds(policy, estimator, regime):
    """The fixed guard for one estimator at one regime.

    ``panel_ratio`` bounds the weighted log ratio of the whole panel. A case
    breaches only when its ratio exceeds ``case_ratio`` **and** its absolute
    delta exceeds the floor (``case_floor_ns`` for time, ``case_floor_bytes`` for
    memory), so jitter on millisecond cases never counts. The screen must clear
    ``screen_fraction`` of the panel band: a short measurement is accepted only
    when it is already well inside the band a full measurement would apply.
    """
    fixed = policy["cost_thresholds"]
    counts = regime_counts(estimator, policy.get("measurement_protocol"))
    if regime not in counts:
        raise Incomplete(f"Cost regime {regime} is not part of the protocol")
    noise_panel = math.log(fixed["panel_ratio"])
    if regime == "screen":
        noise_panel *= fixed["screen_fraction"]
    return {
        "count": counts[regime],
        "noise_panel": noise_panel,
        "case_ratio": fixed["case_ratio"],
        "floor": fixed["case_floor_bytes" if estimator == "memory" else "case_floor_ns"],
    }


def cost_estimate(samples, estimator):
    if not samples:
        raise Incomplete("Missing cost samples")
    if estimator == "memory":
        values = samples
    elif estimator == "timing":
        values = [median(round_) for round_ in samples if round_]
        if len(values) != len(samples):
            raise Incomplete("Empty timing round")
    elif estimator == "companion":
        if set(samples) != {str(s) for s in range(20)}:
            raise Incomplete("Companion requires every seed 0–19")
        return fmean(cost_estimate(rounds, "timing") for rounds in samples.values())
    else:
        raise HarnessError(f"Unknown cost estimator: {estimator}")
    if any(not math.isfinite(x) or x <= 0 for x in values):
        raise Incomplete("Non-positive or non-finite cost sample")
    return median(values)


def _breached(estimate, baseline, weights, threshold):
    ratios = {c: estimate[c] / baseline[c] for c in weights}
    ln_panel = sum(w * math.log(ratios[c]) for c, w in weights.items())
    caps = [
        c
        for c, ratio in ratios.items()
        if ratio > threshold["case_ratio"] and estimate[c] - baseline[c] > threshold["floor"]
    ]
    return {
        "ln_panel": ln_panel,
        "ratios": ratios,
        "cap_breaches": caps,
        "breached": bool(caps) or ln_panel > threshold["noise_panel"],
    }


def validate_bundle(bundle, policy, estimator, weights, run_id=None, requirement=None):
    """Check one regime bundle before it is reused or judged; ``Incomplete`` when it cannot be.

    ``requirement`` is the session's ``cost_evidence``: when present, the bundle must also
    satisfy that extension's evidence check (see ``qtb.extensions``).
    """
    if not bundle.get("complete") or set(bundle.get("arms", {})) != set(MEASURED_ARMS):
        raise Incomplete("Cost panel must contain baseline and evolved arms")
    if run_id is not None and bundle["run_id"] != run_id:
        raise Incomplete("Cost observations cannot cross comparisons")
    arms = bundle["arms"]
    if len({a["arm_id"] for a in arms.values()}) != len(MEASURED_ARMS):
        raise HarnessError("Distinct cost arm IDs required, even for identical builds")
    if any(a["session_id"] != bundle["session_id"] for a in arms.values()):
        raise HarnessError("Mixed cost sessions")
    if bundle.get("thresholds_id") != thresholds_id(policy):
        raise Incomplete("Cost bundle was measured against different thresholds")
    if bundle.get("estimator") != estimator:
        raise Incomplete("Cost bundle estimator does not match the panel")
    count = thresholds(policy, estimator, bundle["regime"])["count"]
    for arm in arms.values():
        if set(arm["samples"]) != set(weights):
            raise Incomplete("Incomplete cost arm")
        for samples in arm["samples"].values():
            arrays = samples.values() if estimator == "companion" else [samples]
            if any(len(x) != count for x in arrays):
                raise Incomplete("Wrong cost sample count")
    check_cost_evidence(
        requirement, bundle, estimator=estimator, count=count, cases=sorted(weights)
    )


def cost_guard(bundle, policy, estimator, weights, run_id=None, requirement=None):
    """Judge one bundle at its regime against the policy's fixed thresholds.

    ``screen``: ``passed`` when the candidate sits inside ``screen_fraction`` of
    the panel band with no cap breach; otherwise ``unresolved`` with
    ``needs_full`` set, and the panel is measured again at full count in a fresh
    session. ``normal``: a breach sets ``needs_rerun``. ``rerun``: decides
    ``failed`` or ``passed_on_rerun``.
    """
    validate_bundle(bundle, policy, estimator, weights, run_id, requirement)
    regime = bundle["regime"]
    threshold = thresholds(policy, estimator, regime)
    estimates = {
        arm: {case: cost_estimate(samples, estimator) for case, samples in row["samples"].items()}
        for arm, row in bundle["arms"].items()
    }
    candidate = _breached(estimates["evolved"], estimates["baseline"], weights, threshold)
    result = "unresolved" if candidate["breached"] else "passed"
    if regime == "rerun":
        result = "failed" if candidate["breached"] else "passed_on_rerun"
    return {
        "result": result,
        "regime": regime,
        "count": threshold["count"],
        "threshold": threshold,
        "candidate": candidate,
        "needs_full": regime == "screen" and result != "passed",
        "needs_rerun": candidate["breached"] and regime == "normal",
    }
