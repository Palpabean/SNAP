"""Command-line entry point. Only `validate` exists so far; the host commands
(status, logs, resume, report) arrive with the engine."""

from __future__ import annotations

import argparse
import sys

from snaplab.core import config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snaplab", description="SNAP lab builder")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="check a snap.yaml file")
    validate.add_argument("file", help="path to snap.yaml")
    args = parser.parse_args(argv)

    if args.command == "validate":
        try:
            config.load(args.file)
        except config.ConfigError as e:
            print(f"{args.file}: {len(e.errors)} problem(s)", file=sys.stderr)
            for err in e.errors:
                print(f"  - {err}", file=sys.stderr)
            return 1
        print(f"{args.file}: valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
