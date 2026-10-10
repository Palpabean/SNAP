"""The `snap` command on the Proxmox host (Design 0001, section 9).

    snap resume   continue setup from the first unfinished step
    snap status   show each stage and step
    snap logs     show the engine log

Started by the snap-resume service after the handoff, and by hand to retry.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys

from snaplab.core.engine import LOG_FILE, STATE_DIR, Context, Engine
from snaplab.core.host import Host
from snaplab.stages.crackle.stage import STAGE as CRACKLE

STAGES = [CRACKLE]
NEXT = "POP (router, networks and test VMs) is not built yet (milestone M3)."


def console(message: str) -> None:
    print(message, flush=True)
    try:
        with open("/dev/console", "w") as c:
            c.write(message + "\n")
    except OSError:
        pass


def make_engine(host: Host, say=console) -> Engine:
    text = host.read(f"{STATE_DIR}/snap.json")
    cfg = json.loads(text) if text else {}
    node = socket.gethostname().split(".")[0]
    return Engine(STAGES, Context(host=host, cfg=cfg, node=node, say=say))


def main(argv: list[str] | None = None, host: Host | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snap", description="SNAP lab setup on this host")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("resume", help="continue setup from the first unfinished step")
    sub.add_parser("status", help="show each stage and step")
    sub.add_parser("logs", help="show the engine log")
    args = parser.parse_args(argv)

    host = host or Host()
    engine = make_engine(host)
    if args.command == "resume":
        if not engine.resume():
            return 1
        console(f"SNAP: {NEXT}")
    elif args.command == "status":
        print("\n".join(engine.status()))
    elif args.command == "logs":
        print(host.read(LOG_FILE) or "(no log yet)", end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
