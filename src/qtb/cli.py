"""Public command-line entry point and stable exit statuses.

Each command does one stage against one session directory (``--results-root``)::

    compile --baseline PATH --evolved PATH --store DIR [--profile P] [--results-root DIR]
    quality | correctness | unit-tests | cost | decide | clean   [--results-root DIR]

Stage exit statuses: 0 complete or skipped, 40 the stage failed, 41 a precondition is not
met, 42 the stage detected interference and ended ``noisy``, 64 usage. ``decide`` exits with
the verdict's status (0, 10, 20, 30 or 40), then cleans the session unless a safety check
refuses; a cleanup problem is reported on stderr and never changes that status.
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
        "decide": "merge the evidence, write the verdict and report, then clean",
        "clean": "delete the session's bulk after decide, keeping its results",
    }
    for name, text in helps.items():
        command = commands.add_parser(name, help=text)
        command.add_argument("--results-root", type=Path, default=Path("results"))
    return cli


def invoke(command, results_root, *, baseline=None, evolved=None, profile=None, store=None):
    """Run one command; returns ``(exit status, message)`` with the CLI's status mapping.

    ``message`` is the problem shown on stderr, or ``None``. Scheduler entry points call this
    under their own execution context (``qtb.execution``).
    """
    try:
        if command == "compile":
            return run_compile(baseline, evolved, profile, results_root, store), None
        if command in STAGE_COMMANDS:
            return run_stage(results_root, command), None
        if command == "decide":
            from qtb import execution
            from qtb.coordinator.clean import after_decide
            from qtb.coordinator.decide import decide

            code, decision = decide(results_root)
            after_decide(
                results_root,
                decision,
                progress=lambda line: print(line, file=sys.stderr),
                cleanup=execution.current().cleanup,
            )
            return code, None
        if command == "clean":
            from qtb.coordinator.clean import clean

            return clean(results_root), None
        raise Usage(f"Unknown command {command}")
    except Usage as exc:
        return EXIT_USAGE, f"USAGE: {exc}"
    except Precondition as exc:
        return EXIT_PRECONDITION, f"PRECONDITION: {exc}"
    except (HarnessError, OSError, ValueError, KeyError) as exc:
        return EXIT_ERROR, f"ERROR: {exc}"


def main(argv=None):
    args = parser().parse_args(argv)
    extra = {}
    if args.command == "compile":
        extra = dict(
            baseline=args.baseline, evolved=args.evolved, profile=args.profile, store=args.store
        )
    code, message = invoke(args.command, args.results_root, **extra)
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
