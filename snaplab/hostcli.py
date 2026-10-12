"""The `snap` command on the Proxmox host (Design 0001, section 9).

    snap resume   continue setup from the first unfinished step
    snap status   show each stage and step
    snap logs     show the engine log
    snap screen   the setup screen on the computer's monitor (the snap-screen service)

Started by the snap-resume service after the handoff, and by hand to retry.
BANG, the last stage, turns that service off once the lab is ready.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from snaplab.core import defaults
from snaplab.core.engine import LOG_FILE, STATE_DIR, Context, Engine
from snaplab.core.host import Host
from snaplab.stages.bang import stage as bang
from snaplab.stages.crackle.stage import STAGE as CRACKLE
from snaplab.stages.pop.stage import STAGE as POP

STAGES = [CRACKLE, POP, bang.STAGE]
# What the setup wizard on the stick saved on the new disk: the choices made on
# its screens, laid over the stick's settings. Kept outside STATE_DIR, which the
# handoff replaces.
WIZARD_SETTINGS = "/etc/snap/wizard.json"
SCREEN_MARK = "/run/snap/screen"  # present while `snap screen` is on the monitor


def consoles(screen_on: bool) -> list[str]:
    """Where progress lines go: the system console, or only its serial lines while the setup
    screen (snap screen) is on the monitor, so they do not draw over it."""
    if not screen_on:
        return ["/dev/console"]
    try:
        with open("/sys/class/tty/console/active") as f:
            active = f.read().split()
    except OSError:
        return []
    return [f"/dev/{name}" for name in active if not name.startswith("tty") or name.startswith("ttyS")]


def console(message: str) -> None:
    print(message, flush=True)
    for path in consoles(os.path.exists(SCREEN_MARK)):
        try:
            with open(path, "w") as c:
                c.write(message + "\n")
        except OSError:
            pass


def make_engine(host: Host, say=console) -> Engine:
    text = host.read(f"{STATE_DIR}/snap.json")
    cfg = json.loads(text) if text else {}
    chosen = host.read(WIZARD_SETTINGS)
    if chosen:
        cfg = defaults.merge(cfg, json.loads(chosen))
    node = socket.gethostname().split(".")[0]
    return Engine(STAGES, Context(host=host, cfg=cfg, node=node, say=say))


def main(argv: list[str] | None = None, host: Host | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snap", description="SNAP lab setup on this host")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("resume", help="continue setup from the first unfinished step")
    sub.add_parser("status", help="show each stage and step")
    sub.add_parser("logs", help="show the engine log")
    sub.add_parser("screen", help="show setup progress full screen (the snap-screen service)")
    args = parser.parse_args(argv)

    host = host or Host()
    engine = make_engine(host)
    if args.command == "resume":
        if not engine.resume():
            return 1
        web = (bang.web_address(host) or "https://<this computer>:8006") + " (user root)"
        console(f"SNAP: Your lab is ready. Open {web}; the summary is in {bang.SUMMARY_TEXT}.")
    elif args.command == "status":
        print("\n".join(engine.status()))
    elif args.command == "screen":
        from snaplab import screen

        return screen.main(engine)
    elif args.command == "logs":
        print(host.read(LOG_FILE) or "(no log yet)", end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
