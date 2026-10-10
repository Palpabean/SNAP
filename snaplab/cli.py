"""Command-line entry point. The host commands (status, logs, resume, report)
arrive with the engine."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from snaplab.core import config
from snaplab.stages.snap import answer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snaplab", description="SNAP lab builder")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="check a snap.yaml file")
    validate.add_argument("file", help="path to snap.yaml")
    render = sub.add_parser("answer", help="write the Proxmox installer answer file for a snap.yaml")
    render.add_argument("file", help="path to snap.yaml")
    render.add_argument("-o", "--output", help="where to write answer.toml (default: print it)")
    usb = sub.add_parser("build", help="build the bootable USB image for a snap.yaml")
    usb.add_argument("file", help="path to snap.yaml")
    usb.add_argument("-o", "--output", default="snap.img", help="image to write (default: snap.img)")
    usb.add_argument("--cache", type=Path, help="download cache (default: $SNAPLAB_CACHE or ~/.cache/snaplab)")
    usb.add_argument(
        "--serial-console", action="store_true", help="also show installer and system output on the first serial port"
    )
    args = parser.parse_args(argv)

    try:
        cfg = config.load(args.file)
    except config.ConfigError as e:
        print(f"{args.file}: {len(e.errors)} problem(s)", file=sys.stderr)
        for err in e.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    if args.command == "validate":
        print(f"{args.file}: valid")
    elif args.command == "build":
        from snaplab.delivery.usb import build, fetch, image

        try:
            out = build.build(Path(args.file), Path(args.output), cache=args.cache, serial_console=args.serial_console)
        except (build.BuildError, fetch.FetchError, image.ImageError) as e:
            print(f"build failed: {e}", file=sys.stderr)
            return 1
        print(f"Write it to a USB stick of 2 GB or more, for example: sudo dd if={out.name} of=/dev/sdX bs=4M")
    elif args.command == "answer":
        text = answer.render(cfg)
        if args.output:
            with open(args.output, "w") as f:
                f.write(text)
        else:
            sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
