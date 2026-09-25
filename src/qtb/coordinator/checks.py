"""Execute the frozen behavioral and Clifford regression suites.

Each suite compiles every configuration concurrently, then sends all oracle requests to the
verifier at once. ``verify_many`` verifies each distinct output once, so seeds and revisions
that reproduce an output already verified cost nothing.
"""

from qtb.canonical import read_circuit, read_json
from qtb.config import STAGES
from qtb.coordinator.storage import append_record
from qtb.evaluator import record

# C6 already replays layout and routing exactly on every scored seed. C7's own contribution is
# translation (and, in full mode, optimization) at full width, which a few seeds exercise as
# well as ten. Acceptance keeps the wider sample.
CLIFFORD_SEEDS = {"IA": 3, "CA": 10}


def _api_ok(result):
    return (
        result["status"] == "ok"
        and result["input_before"] == result["input_after"]
        and (
            result["first"] == result["again"]
            and result["batch_count"] == 2
            and result["batch_hashes"] == result["individual_hashes"]
            and result["first_layout"] == result["again_layout"]
            and result["batch_layouts"] == result["individual_layouts"]
            and result["batch_metadata"] == result["input_metadata"]
            and all(r["exception_type"] == "TranspilerError" for r in result["negative_tests"])
        )
    )


def _plan(comparison, case, result, requests):
    """Checks for one compiled seed; ints stand for the index of a pending oracle request."""
    from qtb.coordinator import structural_result

    if result["status"] != "ok":
        return [{"status": "mismatch", "detail": result.get("error")}]
    structural = structural_result(
        result["output"],
        read_json(comparison.fixtures / case["target"]["file"]),
        result["layout"],
        case["logical_qubits"],
        case["options"].get("initial_layout"),
        case["constraint_form"],
    )
    checks = [dict(structural, oracle="C0")]
    if case["oracle"] == "C4":
        header, _ = read_circuit(comparison.fixtures / case["circuit"]["file"])
        allowed = {p["name"] for p in header["parameters"]}
        if not set(result.get("free_parameters", [])) <= allowed:
            checks.append({"status": "mismatch", "detail": "Output introduced free parameters"})
        outputs = result.get("bindings", [])
        # An optimization can remove every free parameter; the numeric output then serves
        # every declared binding.
        if not result.get("free_parameters"):
            outputs = [result] * len(case["bindings"])
        if len(outputs) != len(case["binding_references"]):
            checks.append({"status": "unverified", "detail": "Missing bound exports"})
        else:
            for bound, reference in zip(outputs, case["binding_references"], strict=True):
                checks.append(len(requests))
                requests.append((case, bound, "C1", reference, {}))
        return checks
    extra = {"controlled": True} if case["oracle"] == "C1" and case["logical_qubits"] <= 2 else {}
    if case["oracle"] == "C5":
        extra = {
            "start_times_dt": result.get("start_times_dt", []),
            "target": str(comparison.fixtures / case["target"]["file"]),
        }
    checks.append(len(requests))
    requests.append((case, result, case["oracle"], None, extra))
    return checks


def behavior_checks(comparison, revisions=("baseline", "evolved")):
    suite = read_json(comparison.fixtures / "correctness-suite.json")
    cases = suite["cases"]
    for revision in revisions:
        subject = "reference" if revision == "baseline" else "evolved"
        comparison.progress(
            f"{revision}: frozen C1–C5 regression suite ({len(cases)} configurations)."
        )
        compiled = comparison.jobs(
            (revision, case, "quality", range(case["seeds_per_block"])) for case in cases
        )
        # API contract checks run independently of metric/oracle grading.
        api_cases = [
            case
            for case in cases
            if case["optimization_level"] == 2 and case["options"]["initial_layout"] is None
        ]
        api = comparison.jobs((revision, case, "api_checks", [0]) for case in api_cases)
        requests, plans = [], []
        for case, results in zip(cases, compiled, strict=True):
            for result in results:
                plans.append((case, result, _plan(comparison, case, result, requests)))
        comparison.progress(f"{revision}: verifying {len(requests)} oracle requests.")
        verdicts = comparison.verify_many(requests)
        statuses = []
        for case, result, checks in plans:
            for check in checks:
                if isinstance(check, int):
                    check = verdicts[check]
                check.update(case_id=case["case_id"], revision=revision, seed=result["seed"])
                append_record(comparison.directory / "correctness.jsonl", check)
                statuses.append(check["status"])
                if check["status"] == "mismatch":
                    comparison.evidence(
                        record(
                            f"behavior/{revision}/{case['case_id']}/{result['seed']}",
                            "correctness",
                            "failed",
                            subject,
                            detail=check.get("detail", case["oracle"]),
                        )
                    )
        for case, rows in zip(api_cases, api, strict=True):
            ok = _api_ok(rows[0])
            statuses.append("verified" if ok else "mismatch")
            if not ok:
                comparison.evidence(
                    record(
                        f"api/{revision}/{case['case_id']}",
                        "correctness",
                        "failed",
                        subject,
                        detail="API contract changed",
                    )
                )
        comparison.evidence(
            record(
                f"{comparison.prefix}1/C1-C5/{revision}",
                "correctness",
                "passed" if statuses and all(s == "verified" for s in statuses) else "unresolved",
                subject,
            )
        )
    required = {f"{comparison.prefix}1/C1-C5/{revision}" for revision in ("baseline", "evolved")}
    passed = {r["id"] for r in comparison.records if r["result"] == "passed"}
    comparison.evidence(
        record(
            f"{comparison.prefix}1/C1-C5",
            "correctness",
            "passed" if required <= passed else "unresolved",
        )
    )


def clifford_checks(comparison, cases):
    seeds = range(CLIFFORD_SEEDS[comparison.prefix])
    modes = (
        ("full", [], STAGES, []),
        (
            "prefix",
            ["drop_stage:optimization", "unitary_synthesis_method=clifford"],
            STAGES[:4],
            ["unitary_synthesis"],
        ),
    )
    specs, labels = [], []
    for revision in ("baseline", "evolved"):
        for case in cases:
            if "clifford_variant" not in case:
                continue
            variant = dict(
                case,
                circuit=case["clifford_variant"],
                input_domain="all_inputs",
                options=dict(case["options"], qubits_initially_zero=False),
            )
            for mode, edits, covers, substituted in modes:
                specs.append((revision, variant, "prefix", seeds, edits))
                labels.append((revision, case, variant, mode, covers, substituted))
    outputs = comparison.jobs(specs)
    requests, planned = [], []
    for (revision, case, variant, mode, covers, substituted), rows in zip(
        labels, outputs, strict=True
    ):
        for output in rows:
            if output["status"] == "ok":
                check = len(requests)
                requests.append(
                    (
                        variant,
                        output,
                        "C7",
                        case["clifford_variant"],
                        {"covers": covers, "substituted": substituted},
                    )
                )
            else:
                check = {"status": "unverified", "detail": output.get("error")}
            planned.append((revision, case, mode, output, check))
    verdicts = comparison.verify_many(requests)
    checks = []
    for revision, case, mode, output, check in planned:
        if isinstance(check, int):
            check = verdicts[check]
        check.update(
            case_id=case["case_id"], revision=revision, seed=output["seed"], mode=mode, oracle="C7"
        )
        append_record(comparison.directory / "clifford.jsonl", check)
        if mode == "prefix":
            checks.append(check)
        if check["status"] == "mismatch":
            comparison.evidence(
                record(
                    f"C7/{revision}/{case['case_id']}/{mode}/{output['seed']}",
                    "correctness",
                    "failed",
                    "reference" if revision == "baseline" else "evolved",
                )
            )
    comparison.evidence(
        record(
            f"{comparison.prefix}1/C7",
            "correctness",
            "passed" if checks and all(c["status"] == "verified" for c in checks) else "unresolved",
        )
    )
