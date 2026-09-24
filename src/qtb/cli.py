"""Public command-line entry point and stable exit statuses."""

import argparse
import sys
from pathlib import Path

from qtb.canonical import read_json
from qtb.coordinator import Comparison
from qtb.errors import HarnessError
from qtb.evaluator import EXIT_CODES


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, f"{self.prog}: error: {message}\n")


def parser():
    cli = Parser(prog="qiskit-transpile-bench")
    commands = cli.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("compare", "smoke"):
        command = commands.add_parser(name)
        command.add_argument("--baseline", required=True, type=Path)
        command.add_argument("--evolved", required=True, type=Path)
        command.add_argument(
            "--profile",
            choices=["iterations-profile", "confirm-profile"],
            default="iterations-profile",
        )
        command.add_argument("--results-root", type=Path, default=Path("results"))
        command.add_argument("--resume", type=Path)
        command.add_argument("--change-scope", type=Path)
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command in {"compare", "smoke"}:
            scope = read_json(args.change_scope)["stages"] if args.change_scope else []
            comparison = Comparison(
                args.baseline, args.evolved, args.profile, args.results_root, args.resume, scope
            )
            result = comparison.execute(smoke=args.command == "smoke")
            if args.command == "smoke":
                print(f"SMOKE {'OK' if result['success'] else 'FAILED'}: {comparison.directory}")
                if result.get("error"):
                    print(result["error"], file=sys.stderr)
                return 0 if result["success"] else 40
            print(f"{result['status']} ({args.profile}): {comparison.directory}")
            return EXIT_CODES[result["status"]]
        raise HarnessError("Unknown command")
    except (HarnessError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 40


if __name__ == "__main__":
    raise SystemExit(main())
