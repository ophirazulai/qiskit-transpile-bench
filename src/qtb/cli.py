"""Public command-line entry point and stable exit statuses."""

import argparse
import sys
from pathlib import Path

from qtb.canonical import read_json, write_json
from qtb.coordinator import Comparison, evaluate_run
from qtb.coordinator.storage import read_records
from qtb.errors import HarnessError
from qtb.evaluator import EXIT_CODES
from qtb.reporter import review_decision, write_report


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, f"{self.prog}: error: {message}\n")


def parser():
    cli = Parser(prog="qiskit-transpile-bench")
    commands = cli.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("compare", "smoke", "calibrate"):
        command = commands.add_parser(name)
        command.add_argument("--baseline", required=True, type=Path)
        command.add_argument("--evolved", required=name != "calibrate", type=Path)
        command.add_argument(
            "--profile",
            choices=["iterations-profile", "confirm-profile"],
            default="iterations-profile",
        )
        command.add_argument("--results-root", type=Path, default=Path("results"))
        command.add_argument("--resume", type=Path)
        command.add_argument("--change-scope", type=Path)
    for name in ("evaluate", "report", "derive-exclusions"):
        command = commands.add_parser(name)
        command.add_argument("run", type=Path)
    review = commands.add_parser("review")
    review.add_argument("--decision", type=Path, required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--rationale", type=Path, required=True)
    review.add_argument("--outcome", choices=["reviewed_accept", "reviewed_reject"], required=True)
    repro = commands.add_parser("repro")
    repro.add_argument("observation_id")
    repro.add_argument("--run", type=Path, required=True)
    repro.add_argument("--out", type=Path, default=Path("repro"))
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command in {"compare", "smoke", "calibrate"}:
            scope = read_json(args.change_scope)["stages"] if args.change_scope else []
            comparison = Comparison(
                args.baseline, args.evolved, args.profile, args.results_root, args.resume, scope
            )
            if args.command == "calibrate":
                from qtb.coordinator.calibration import preflight

                comparison.build(need_evolved=False)
                cases = [
                    c for c in comparison.manifest["cases"] if c["role"] not in {"timing", "memory"}
                ]
                comparison.roundtrip(comparison.roundtrip_cases(), revisions=("baseline",))
                summary = preflight(comparison, cases)
                write_json(comparison.directory / "calibration.json", summary)
                print(f"Calibration evidence: {comparison.directory}")
                return (
                    0
                    if summary["quality"]["freeze_allowed"]
                    and summary["false_rejection"]["freeze_allowed"]
                    and all(c["freeze_allowed"] for c in summary["cost"].values())
                    else 40
                )
            result = comparison.execute(smoke=args.command == "smoke")
            if args.command == "smoke":
                print(f"SMOKE {'OK' if result['success'] else 'FAILED'}: {comparison.directory}")
                if result.get("error"):
                    print(result["error"], file=sys.stderr)
                return 0 if result["success"] else 40
            print(f"{result['status']} ({args.profile}): {comparison.directory}")
            return EXIT_CODES[result["status"]]
        if args.command == "evaluate":
            decision = evaluate_run(args.run)
            print(f"{decision['status']} ({decision['profile']})")
            return EXIT_CODES[decision["status"]]
        if args.command == "derive-exclusions":
            from qtb.config import data_root
            from qtb.coordinator.upstream import derive_exclusions

            proposal = derive_exclusions(args.run, data_root() / "envs")
            proposal_path = args.run / "exclusion-derivation/exclusions.proposed.json"
            print(f"Unreviewed exclusion proposal: {proposal_path}")
            return (
                0
                if proposal["ordinary"]["result"] == "passed"
                and proposal["reshuffled"]["result"] != "unresolved"
                else 40
            )
        if args.command == "report":
            decision = read_json(args.run / "decision.json")
            write_report(args.run, decision)
            print(args.run / "report.md")
            return 0
        if args.command == "review":
            result = review_decision(
                args.decision, args.reviewer, args.rationale.read_text(), args.outcome
            )
            print(result["outcome"])
            return 0
        if args.command == "repro":
            rows = read_records(args.run / "observations.jsonl")
            observation = next((r for r in rows if r["id"] == args.observation_id), None)
            if observation is None:
                raise HarnessError("Observation not found in this run")
            from qtb.repro import export_reproducer

            export_reproducer(observation, args.run, args.out)
            print(f"Reproducer: {args.out / 'run.py'}")
            return 0
        raise HarnessError("Unknown command")
    except (HarnessError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 40


if __name__ == "__main__":
    raise SystemExit(main())
