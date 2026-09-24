"""Estimator-specific A/A calibration and three-arm cost guards."""

import math
import random
from datetime import datetime, timedelta
from statistics import fmean, median

from qtb.canonical import digest
from qtb.errors import HarnessError, Incomplete
from qtb.evaluator.statistics import quantile

# Fallback (screen, normal, rerun, collected) counts when a policy names none.
COUNTS = {"timing": (0, 10, 20, 30), "companion": (0, 3, 6, 30), "memory": (0, 5, 10, 10)}


def regime_counts(estimator, protocol=None):
    """Per-regime sample counts for one estimator, from the measurement protocol.

    A ``screen`` count of zero (or a missing key) disables the short early-stop
    regime. ``normal`` is the full measurement; ``rerun`` is the fresh doubled
    measurement after a candidate-only breach; ``collected`` is the A/A
    calibration sample that every threshold is bootstrapped from.
    """
    if estimator not in COUNTS:
        raise HarnessError(f"Unknown cost estimator: {estimator}")
    screen, normal, rerun, collected = COUNTS[estimator]
    protocol = protocol or {}
    multiplier = protocol.get("rerun_multiplier", 2)
    if estimator == "timing":
        normal = protocol.get("timing_rounds", normal)
        screen = protocol.get("screen_rounds", screen)
        collected = protocol.get("calibration_rounds", collected)
    elif estimator == "companion":
        normal = protocol.get("companion_rounds", normal)
        collected = protocol.get("calibration_rounds", collected)
    else:
        normal = protocol.get("memory_processes", normal)
        collected = protocol.get("calibration_memory_processes", collected)
    rerun = normal * multiplier
    if not 0 <= screen < normal < rerun or collected < rerun:
        raise HarnessError(
            f"Invalid {estimator} regime counts: {(screen, normal, rerun, collected)}"
        )
    regimes = {"normal": normal, "rerun": rerun}
    if screen:
        regimes = {"screen": screen, **regimes}
    return regimes, collected


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


def _resample(samples, estimator, indices):
    if estimator == "companion":
        return {seed: [rounds[i] for i in indices] for seed, rounds in samples.items()}
    return [samples[i] for i in indices]


def calibrate_cost(
    arms,
    estimator,
    weights,
    machine,
    created_at,
    rng_seed=20260924,
    replicates=1000,
    protocol=None,
):
    """arms = {baseline/control: {case: raw rounds/processes}}.

    Round indices are shared across cases and companion seeds to preserve session
    blocks. Inner timed calls stay intact. All draws are with replacement. Every
    regime the protocol names (screen, normal, rerun) gets its own noise band and
    per-case floors at that regime's sample count.
    """
    if set(arms) != {"baseline", "control"} or not weights:
        raise HarnessError("A/A calibration needs two independent baseline arms")
    counts, collected = regime_counts(estimator, protocol)
    if not math.isclose(sum(weights.values()), 1.0):
        raise HarnessError("Cost weights must sum to one")
    for cases in arms.values():
        if set(cases) != set(weights):
            raise Incomplete("Incomplete calibration panel")
        for samples in cases.values():
            arrays = samples.values() if estimator == "companion" else [samples]
            if any(len(x) != collected for x in arrays):
                raise Incomplete(f"Calibration requires {collected} records per arm")
            cost_estimate(samples, estimator)
    regimes = {}
    rng = random.Random(rng_seed)
    for regime, count in counts.items():
        panels, absolute = [], {c: [] for c in weights}
        for _ in range(replicates):
            draws = {arm: rng.choices(range(collected), k=count) for arm in arms}
            ln_panel = 0.0
            for case, weight in weights.items():
                a, b = [
                    cost_estimate(_resample(arms[arm][case], estimator, draws[arm]), estimator)
                    for arm in ("baseline", "control")
                ]
                ln_panel += weight * math.log(b / a)
                absolute[case].append(abs(b - a))
            panels.append(abs(ln_panel))
        noise = max(math.log(1.01), quantile(panels, 0.95))
        regimes[regime] = {
            "count": count,
            "noise_panel": noise,
            "floors": {c: quantile(v, 0.95) for c, v in absolute.items()},
        }
    result = {
        "estimator": estimator,
        "weights": weights,
        "regimes": regimes,
        "collected": collected,
        "machine": machine,
        "created_at": created_at,
        "rng_seed": rng_seed,
        "raw_hash": digest(arms),
        "replicates": replicates,
        "freeze_allowed": all(r["noise_panel"] <= math.log(1.05) for r in regimes.values()),
    }
    result["null_rejections"] = null_cost_rejections(arms, result, replicates, rng_seed + 1)
    rate = sum(result["null_rejections"]) / replicates
    result["false_rejection_rate"] = rate
    result["monte_carlo_SE"] = math.sqrt(rate * (1 - rate) / replicates)
    result["id"] = digest(result)
    return result


def threshold_for(calibration, regime):
    """The screen is judged against the full measurement's noise band.

    The short regime's own band is wider, so an estimate that already sits inside
    the full band is one a full run would also accept with high probability. Per
    case floors stay the short regime's own, since they bound absolute noise at
    that sample count.
    """
    threshold = calibration["regimes"][regime]
    if regime == "screen":
        threshold = dict(threshold, noise_panel=calibration["regimes"]["normal"]["noise_panel"])
    return threshold


def _breached(estimate, baseline, weights, threshold):
    ratios = {c: estimate[c] / baseline[c] for c in weights}
    ln_panel = sum(w * math.log(ratios[c]) for c, w in weights.items())
    caps = [
        c
        for c, ratio in ratios.items()
        if ratio > 1.1 and estimate[c] - baseline[c] > threshold["floors"][c]
    ]
    return {
        "ln_panel": ln_panel,
        "ratios": ratios,
        "cap_breaches": caps,
        "breached": bool(caps) or ln_panel > threshold["noise_panel"],
    }


def null_cost_rejections(arms, calibration, replicates=1000, rng_seed=20260925):
    """Bootstrap the complete three-arm rule: screen, full measurement, fresh rerun.

    Cases share round draws within each arm. The two independently built
    baseline series form the null distribution; the candidate is another
    independent draw from the control series. Repeated trials retain all
    within-round calls and all companion seeds. Each regime is a fresh draw,
    as each is a fresh session on the runner.
    """
    estimator, weights = calibration["estimator"], calibration["weights"]
    collected = calibration.get("collected", COUNTS[estimator][3])
    rng = random.Random(rng_seed)

    def trial(regime):
        threshold = threshold_for(calibration, regime)
        estimates = {}
        for arm in ("baseline", "control", "evolved"):
            indices = rng.choices(range(collected), k=threshold["count"])
            source = arms["baseline" if arm == "baseline" else "control"]
            estimates[arm] = {
                case: cost_estimate(_resample(source[case], estimator, indices), estimator)
                for case in weights
            }
        return tuple(
            _breached(estimates[arm], estimates["baseline"], weights, threshold)["breached"]
            for arm in ("control", "evolved")
        )

    rejected = []
    for _ in range(replicates):
        if "screen" in calibration["regimes"]:
            control, candidate = trial("screen")
            if not control and not candidate:
                rejected.append(False)
                continue
        control, candidate = trial("normal")
        failure = False
        if candidate and not control:
            control, candidate = trial("rerun")
            failure = candidate and not control
        rejected.append(failure)
    return rejected


def validate_bundle(bundle, calibration, run_id=None, historical=False):
    if not bundle.get("complete") or set(bundle.get("arms", {})) != {
        "baseline",
        "control",
        "evolved",
    }:
        raise Incomplete("Cost panel must contain three complete arms")
    if run_id is not None and bundle["run_id"] != run_id:
        raise Incomplete("Cost observations cannot cross comparisons")
    arms = bundle["arms"]
    if len({a["arm_id"] for a in arms.values()}) != 3:
        raise HarnessError("Distinct cost arm IDs required, even for identical builds")
    if any(a["session_id"] != bundle["session_id"] for a in arms.values()):
        raise HarnessError("Mixed cost sessions")
    if bundle["calibration_id"] != calibration["id"] or bundle["machine"] != calibration["machine"]:
        raise Incomplete("Cost calibration does not match bundle")
    if not calibration["freeze_allowed"]:
        raise Incomplete("Runner exceeds the calibration freeze limit")
    measured = datetime.fromisoformat(bundle["measured_at"])
    created = datetime.fromisoformat(calibration["created_at"])
    if not created <= measured <= created + timedelta(days=30):
        raise Incomplete("Calibration was stale at measurement time")
    if not historical and datetime.now(created.tzinfo) > created + timedelta(days=30):
        raise Incomplete("Calibration has expired")
    if bundle["regime"] not in calibration["regimes"]:
        raise Incomplete(f"Cost regime {bundle['regime']} was not calibrated")
    count = calibration["regimes"][bundle["regime"]]["count"]
    for arm in arms.values():
        if set(arm["samples"]) != set(calibration["weights"]):
            raise Incomplete("Incomplete cost arm")
        for samples in arm["samples"].values():
            arrays = samples.values() if calibration["estimator"] == "companion" else [samples]
            if any(len(x) != count for x in arrays):
                raise Incomplete("Wrong cost sample count")


def cost_guard(bundle, calibration, run_id=None, historical=True):
    """Judge one bundle at its regime.

    ``screen``: ``passed`` when the control is clean and the candidate sits inside
    the full-count noise band with no cap breach; otherwise ``unresolved`` with
    ``needs_full`` set, and the panel is measured again at full count in a fresh
    session. ``normal``: a candidate-only breach sets ``needs_rerun``. ``rerun``:
    decides ``failed`` or ``passed_on_rerun`` unless the control also breached.
    """
    validate_bundle(bundle, calibration, run_id, historical)
    regime = bundle["regime"]
    threshold = threshold_for(calibration, regime)
    weights = calibration["weights"]
    estimates = {
        arm: {
            case: cost_estimate(samples, calibration["estimator"])
            for case, samples in row["samples"].items()
        }
        for arm, row in bundle["arms"].items()
    }
    control, candidate = (
        _breached(estimates[arm], estimates["baseline"], weights, threshold)
        for arm in ("control", "evolved")
    )
    result = "unresolved" if control["breached"] or candidate["breached"] else "passed"
    if regime == "rerun" and not control["breached"]:
        result = "failed" if candidate["breached"] else "passed_on_rerun"
    return {
        "result": result,
        "regime": regime,
        "count": threshold["count"],
        "control": control,
        "candidate": candidate,
        "needs_full": regime == "screen" and result != "passed",
        "needs_rerun": candidate["breached"] and not control["breached"] and regime == "normal",
    }
