"""Execute the frozen behavioral and Clifford regression suites."""

from qtb.canonical import read_circuit, read_json
from qtb.config import STAGES
from qtb.coordinator.storage import append_record
from qtb.evaluator import record


def behavior_checks(comparison, revisions=("baseline", "evolved")):
    suite = read_json(comparison.fixtures / "correctness-suite.json")
    statuses = []
    for revision in revisions:
        comparison.progress(
            f"{revision}: frozen C1–C5 regression suite ({len(suite['cases'])} configurations)."
        )
        for case in suite["cases"]:
            results = comparison.job(revision, case, "quality", range(case["seeds_per_block"]))
            for result in results:
                checks = []
                if result["status"] != "ok":
                    checks.append({"status": "mismatch", "detail": result.get("error")})
                elif case["oracle"] == "C4":
                    header, _ = read_circuit(comparison.fixtures / case["circuit"]["file"])
                    allowed = {p["name"] for p in header["parameters"]}
                    if not set(result.get("free_parameters", [])) <= allowed:
                        checks.append(
                            {"status": "mismatch", "detail": "Output introduced free parameters"}
                        )
                    outputs = result.get("bindings", [])
                    # An optimization can remove every free parameter; the numeric output
                    # then serves every declared binding.
                    if not result.get("free_parameters"):
                        outputs = [result] * len(case["bindings"])
                    if len(outputs) != len(case["binding_references"]):
                        checks.append({"status": "unverified", "detail": "Missing bound exports"})
                    else:
                        for bound, reference in zip(
                            outputs, case["binding_references"], strict=True
                        ):
                            checks.append(comparison.oracle(case, bound, "C1", reference))
                else:
                    extra = (
                        {"controlled": True}
                        if case["oracle"] == "C1" and case["logical_qubits"] <= 2
                        else {}
                    )
                    if case["oracle"] == "C5":
                        extra = {
                            "start_times_dt": result.get("start_times_dt", []),
                            "target": str(comparison.fixtures / case["target"]["file"]),
                        }
                    checks.append(comparison.oracle(case, result, case["oracle"], **extra))
                for check in checks:
                    check.update(case_id=case["case_id"], revision=revision, seed=result["seed"])
                    append_record(comparison.directory / "correctness.jsonl", check)
                    statuses.append(check["status"])
                    if check["status"] == "mismatch":
                        comparison.evidence(
                            record(
                                f"behavior/{revision}/{case['case_id']}/{result['seed']}",
                                "correctness",
                                "failed",
                                "reference" if revision == "baseline" else "evolved",
                                detail=check.get("detail", case["oracle"]),
                            )
                        )
            # API contract checks run independently of metric/oracle grading.
            if case["optimization_level"] == 2 and case["options"]["initial_layout"] is None:
                result = comparison.job(revision, case, "api_checks", [0])[0]
                ok = (
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
                statuses.append("verified" if ok else "mismatch")
                if not ok:
                    comparison.evidence(
                        record(
                            f"api/{revision}/{case['case_id']}",
                            "correctness",
                            "failed",
                            "reference" if revision == "baseline" else "evolved",
                            detail="API contract changed",
                        )
                    )
    comparison.evidence(
        record(
            f"{comparison.prefix}1/C1-C5",
            "correctness",
            "passed" if statuses and all(s == "verified" for s in statuses) else "unresolved",
        )
    )


def clifford_checks(comparison, cases):
    checks = []
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
            for mode, edits, covers, substituted in (
                ("full", [], STAGES, []),
                (
                    "prefix",
                    ["drop_stage:optimization", "unitary_synthesis_method=clifford"],
                    STAGES[:4],
                    ["unitary_synthesis"],
                ),
            ):
                for output in comparison.job(revision, variant, "prefix", range(10), edits):
                    check = (
                        comparison.oracle(
                            variant,
                            output,
                            "C7",
                            case["clifford_variant"],
                            covers=covers,
                            substituted=substituted,
                        )
                        if output["status"] == "ok"
                        else {"status": "unverified", "detail": output.get("error")}
                    )
                    check.update(
                        case_id=case["case_id"], revision=revision, seed=output["seed"], mode=mode
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
