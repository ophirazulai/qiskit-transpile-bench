"""Public command-line entry point and stable exit statuses.

Each command does one stage against one session directory (``--results-root``)::

    compile --baseline PATH --evolved PATH --store DIR [--profile P] [--results-root DIR]
    quality | correctness | unit-tests | cost | decide | clean   [--results-root DIR]

Stage exit statuses: 0 complete or skipped, 40 the stage failed, 41 a precondition is not
met, 64 usage. ``decide`` exits with the verdict's status (0, 10, 20, 30 or 40).
"""

import argparse
import sys
from pathlib import Path

from qtb.coordinator.stages import (
    EXIT_ERROR,
    EXIT_PRECONDITION,
    EXIT_USAGE,
    run_compile,
    run_stage,
)
from qtb.errors import HarnessError, Precondition, Usage

STAGE_COMMANDS = ("quality", "correctness", "unit-tests", "cost")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def parser():
    cli = Parser(prog="qiskit-transpile-bench")
    commands = cli.add_subparsers(dest="command", required=True, parser_class=Parser)
    compile_ = commands.add_parser(
        "compile", help="create the session, build both revisions, round-trip the inputs"
    )
    compile_.add_argument("--baseline", required=True, type=Path)
    compile_.add_argument("--evolved", required=True, type=Path)
    compile_.add_argument(
        "--store", type=Path, help="baseline store (required unless QTB_STORE is set)"
    )
    compile_.add_argument(
        "--profile",
        choices=["iterations-profile", "confirm-profile"],
        default="iterations-profile",
    )
    compile_.add_argument("--results-root", type=Path, default=Path("results"))
    helps = {
        "quality": "quality compiles, C0, C6, C1-lite, the audit and the gate",
        "correctness": "C1-C5, API contracts and C7 (only when the gate is open)",
        "unit-tests": "optional upstream Python and Rust tests (only when the gate is open)",
        "cost": "timing and memory panels on a quiet host (gate open, correctness passed)",
        "decide": "merge the evidence and write the verdict and report",
        "clean": "delete the session's bulk after decide, keeping its results",
    }
    for name, text in helps.items():
        command = commands.add_parser(name, help=text)
        command.add_argument("--results-root", type=Path, default=Path("results"))
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "compile":
            return run_compile(
                args.baseline, args.evolved, args.profile, args.results_root, args.store
            )
        if args.command in STAGE_COMMANDS:
            return run_stage(args.results_root, args.command)
        if args.command == "decide":
            from qtb.coordinator.decide import decide

            code, _ = decide(args.results_root)
            return code
        if args.command == "clean":
            from qtb.coordinator.clean import clean

            return clean(args.results_root)
        raise Usage(f"Unknown command {args.command}")
    except Usage as exc:
        print(f"USAGE: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Precondition as exc:
        print(f"PRECONDITION: {exc}", file=sys.stderr)
        return EXIT_PRECONDITION
    except (HarnessError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
