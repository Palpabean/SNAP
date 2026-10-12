"""The lab computer's own screen while SNAP sets it up, and once the lab is ready.

    snap screen   runs on the computer's main screen (tty1, the snap-screen service)

Three screens, picked from what the engine records (state.json and the
progress markers), redrawn every two seconds:

  setting up       each stage with a tick, the current step and a progress bar
  lab ready        the web address, user name and password, the networks and machines
  needs attention  what stopped, and keys to try again, show the log or turn off

Plain text with a few colours, so it works on any monitor. Standard library
only (runs on the Proxmox host).
"""

from __future__ import annotations

import ipaddress
import json
import os
import select
import shutil
import sys
import termios
import time
import tty
from typing import Any

from snaplab.core import logo
from snaplab.core.engine import LOG_FILE, PROGRESS_DIR, Engine
from snaplab.core.host import CommandError
from snaplab.stages.pop import addresses, lab

PASSWORD_FILE = "/etc/snap/password"  # written by the setup wizard on the stick
WEB_PORT = 8006

# What each stage does, in plain words (the stage titles carry the code names).
STAGES = [
    ("snap", "SNAP", "Install Proxmox"),
    ("crackle", "CRACKLE", "Prepare this computer"),
    ("pop", "POP", "Build the router, networks and machines"),
    ("bang", "BANG", "Final checks (coming in a later version)"),
]
POLICIES = {
    "internet-only": "internet only",
    "isolated": "isolated",
    "open": "internet and other lab networks",
}
MINUTES_LEFT = {"snap": 40, "crackle": 35, "pop": 25}


# --- what the screen shows ------------------------------------------------------


def web_address(host) -> str | None:
    """https://<this computer's address>:8006, from the main bridge (vmbr0)."""
    try:
        found = json.loads(host.run("ip", "-j", "-4", "addr", "show", "vmbr0"))
    except (CommandError, ValueError):
        return None
    for link in found:
        for a in link.get("addr_info", []):
            if a.get("family") == "inet" and a.get("local"):
                return f"https://{a['local']}:{WEB_PORT}"
    return None


def lab_summary(host, cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The networks and machines as built (with any addresses POP moved), as display lines."""
    text = host.read(addresses.PLAN_FILE)
    if text:
        cfg = addresses.apply(cfg, json.loads(text)["placed"])
    networks = [
        f"{n['name']:<10} {n['cidr']:<16} {POLICIES.get(n.get('policy', ''), n.get('policy', ''))}"
        for n in cfg.get("networks", [])
    ]
    try:
        user = cfg["machines"]["username"]
        machines = [f"{m['name']:<10} {m['ip']:<16} user {user}" for m in lab.machines(cfg)]
    except (KeyError, StopIteration, ValueError, ipaddress.AddressValueError):
        machines = []
    return networks, machines


def snapshot(engine: Engine) -> dict[str, Any]:
    """Everything the screen needs, read fresh from the machine."""
    host = engine.host
    state = engine.load()
    stages = []
    for name, label, what in STAGES:
        record = state["stages"].get(name, {})
        if host.exists(f"{PROGRESS_DIR}/{name}.done"):
            status = "done"
        elif record.get("status") in ("running", "failed"):
            status = record["status"]
        else:
            status = "waiting"
        stages.append({"name": name, "label": label, "what": what, "status": status, "error": record.get("error")})

    current = None
    for stage in engine.stages:
        record = state["stages"].get(stage.name, {})
        if record.get("status") not in ("running", "failed") or host.exists(f"{PROGRESS_DIR}/{stage.name}.done"):
            continue
        steps = [
            {"title": s.title, "status": record.get("steps", {}).get(s.name, {}).get("status", "waiting")}
            for s in stage.steps
        ]
        at = next((i for i, s in enumerate(steps) if s["status"] in ("running", "failed")), None)
        if at is None:
            at = sum(1 for s in steps if s["status"] == "done")
        current = {"stage": stage.name, "steps": steps, "at": min(at, len(steps) - 1)}
        break

    built = [s for s in stages if s["name"] in ("snap", "crackle", "pop")]
    if any(s["status"] == "failed" for s in stages):
        phase = "failed"
    elif all(s["status"] == "done" for s in built):
        phase = "ready"
    else:
        phase = "working"
    networks, machines = lab_summary(host, engine.ctx.cfg)
    password = (host.read(PASSWORD_FILE) or "").strip() or None
    cfg_host = engine.ctx.cfg.get("host", {})
    return {
        "phase": phase,
        "stages": stages,
        "current": current,
        "web": web_address(host),
        "name": f"{engine.ctx.node}.{cfg_host['domain']}" if cfg_host.get("domain") else engine.ctx.node,
        "password": password,
        "networks": networks,
        "machines": machines,
        "minutes": sum(MINUTES_LEFT.get(s["name"], 0) for s in stages if s["status"] in ("waiting", "running")),
    }


# --- drawing ----------------------------------------------------------------------

COLOURS = {"ok": "32", "now": "36", "bad": "31", "dim": "2", "bold": "1", "spark": "34"}


def paint(text: str, colour: str, colour_on: bool) -> str:
    return f"\033[{COLOURS[colour]}m{text}\033[0m" if colour_on else text


def render(snap: dict[str, Any], width: int = 80, height: int = 25, colour: bool = True, show_password: bool = False):
    """The screen as a list of lines, at most `height` long."""
    c = lambda text, kind: paint(text, kind, colour)  # noqa: E731
    lines: list[str] = []
    if height >= 40 and width >= 60:  # room for the logo
        for row in range(len(logo.ART)):
            lines.append("  " + "".join(c(t, "spark") if blue else t for t, blue in logo.runs(row)))
        lines.append("")
    title = "S N A P   S E T U P"
    lines.append(c(title, "now") + " " * max(2, width - len(title) - len(snap["name"]) - 2) + snap["name"])
    lines.append(c("-" * min(width, 78), "dim"))

    def checklist():
        marks = {"done": ("[x]", "ok"), "running": ("[>]", "now"), "failed": ("[!]", "bad"), "waiting": ("[ ]", "dim")}
        for s in snap["stages"]:
            mark, kind = marks[s["status"]]
            what = s["what"]
            if s["status"] == "failed" and snap["current"]:
                cur = snap["current"]
                what = f"Stopped at step {cur['at'] + 1} of {len(cur['steps'])}: {cur['steps'][cur['at']]['title']}"
            label = f"{mark} {s['label']:<8}"
            lines.append(f"  {c(label, kind)} {c(what, 'dim') if s['status'] == 'waiting' else what}")

    password = snap["password"] if snap["password"] else "the one from your setup (or snap.img.password.txt)"

    if snap["phase"] == "working":
        left = f"about {snap['minutes']} minutes left" if snap["minutes"] else "almost done"
        lines += ["", f"Setting up your lab. Keep this computer on ({left}).", ""]
        checklist()
        cur = snap["current"]
        if cur:
            steps, at = cur["steps"], cur["at"]
            label = next(s["label"] for s in snap["stages"] if s["name"] == cur["stage"])
            done = sum(1 for s in steps if s["status"] == "done")
            bar = 40
            filled = round(bar * done / len(steps))
            lines += [
                "",
                f"  {label}, step {at + 1} of {len(steps)}:  {c(steps[at]['title'], 'bold')}",
                f"  [{c('#' * filled, 'now')}{'.' * (bar - filled)}]  {done} / {len(steps)}",
            ]
        lines += ["", c("  [L] Show log   [P] Show password   [O] Turn off", "dim")]
        if snap["web"]:
            lines.append(c(f"  Web page when finished: {snap['web']}", "dim"))
        if show_password:
            lines.append(f"  Password: {password}")

    elif snap["phase"] == "ready":
        lines += ["", f"{c('Your lab is ready.', 'ok')}  You can remove the USB stick.", ""]
        lines.append(f"  Open in your browser:   {c(snap['web'] or 'https://<this computer>:8006', 'ok')}")
        lines.append("  User name:              root")
        lines.append(f"  Password:               {password}")
        lines.append("")
        lines.append(f"  Router                 {lab.ROUTER_NAME}")
        for i, n in enumerate(snap["networks"]):
            lines.append(f"  {'Networks' if i == 0 else '':<22} {n}")
        for i, m in enumerate(snap["machines"]):
            lines.append(f"  {'Machines' if i == 0 else '':<22} {m}")
        lines += [
            "",
            c("  Your browser will warn about the connection: choose Advanced, then Continue.", "dim"),
            "",
            c("  [L] Show log   [O] Turn off   Alt+F2: log in here", "dim"),
        ]

    else:  # failed
        stage = next(s for s in snap["stages"] if s["status"] == "failed")
        lines += ["", f"{c(stage['label'] + ' needs attention.', 'bad')}  Nothing was lost; SNAP stopped safely.", ""]
        checklist()
        reason = (stage["error"] or "").split(": ", 1)[-1]
        lines += ["", "  What happened:"]
        lines += [f"    {part}" for part in wrap(reason, width - 6)[:6]]
        lines += [
            "",
            "  What to try:   press R to try again. SNAP continues from this step.",
            "                 If it fails again, press L and note the last lines.",
            "",
            c("  [R] Try again   [L] Show log   [O] Turn off", "bold"),
        ]
    return lines[:height]


def wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return out + ([line] if line else [])


# --- the screen itself ------------------------------------------------------------


class Screen:
    """Draws on a terminal and answers single key presses."""

    def __init__(self, engine: Engine, out=None, keys_fd: int | None = None, run=os.system):
        self.engine, self.out, self.run = engine, out or sys.stdout, run
        self.keys_fd = sys.stdin.fileno() if keys_fd is None else keys_fd
        self.show_password = False
        self.confirm_off = False

    def size(self) -> tuple[int, int]:
        cols, rows = shutil.get_terminal_size((80, 25))
        return cols, rows

    def draw(self, lines: list[str]) -> None:
        self.out.write("\033[H\033[2J" + "\n".join(lines) + "\n")
        self.out.flush()

    def frame(self) -> list[str]:
        cols, rows = self.size()
        snap = snapshot(self.engine)
        lines = render(snap, cols, rows - 2, show_password=self.show_password)
        if self.confirm_off:
            lines += ["", paint("  Turn the computer off now? Press Y for yes, any other key for no.", "bad", True)]
        return lines

    def key(self, k: str) -> bool:
        """Handle one key; False to stop."""
        k = k.lower()
        if self.confirm_off:
            self.confirm_off = False
            if k == "y":
                self.run("systemctl poweroff")
                return False
            return True
        if k == "p":
            self.show_password = not self.show_password
        elif k == "o":
            self.confirm_off = True
        elif k == "r" and snapshot(self.engine)["phase"] == "failed":
            # The resume service runs `snap resume`; restarting it continues from the failed step.
            self.run("systemctl restart --no-block snap-resume.service")
        elif k == "l":
            self.show_log()
        return True

    def show_log(self) -> None:
        cols, rows = self.size()
        text = self.engine.host.read(LOG_FILE) or "(no log yet)"
        tail = [line[:cols] for line in text.splitlines()[-(rows - 3) :]]
        self.draw([paint("SNAP log (last lines). Press any key to go back.", "now", True), *tail])
        self.wait_key(None)

    def wait_key(self, timeout: float | None) -> str | None:
        ready, _, _ = select.select([self.keys_fd], [], [], timeout)
        if not ready:
            return None
        return os.read(self.keys_fd, 1).decode(errors="replace")

    def loop(self) -> None:
        from snaplab.hostcli import SCREEN_MARK  # progress lines stay off the monitor while this runs

        self.engine.host.write(SCREEN_MARK, f"{os.getpid()}\n")
        old = None
        if os.isatty(self.keys_fd):
            old = termios.tcgetattr(self.keys_fd)
            tty.setcbreak(self.keys_fd)
        self.out.write("\033[?25l")  # hide the cursor
        try:
            while True:
                self.draw(self.frame())
                k = self.wait_key(2.0)
                if k and not self.key(k):
                    break
        finally:
            self.engine.host.path(SCREEN_MARK).unlink(missing_ok=True)
            self.out.write("\033[?25h")
            if old is not None:
                termios.tcsetattr(self.keys_fd, termios.TCSADRAIN, old)


def main(engine: Engine) -> int:
    while True:
        try:
            Screen(engine).loop()
            return 0
        except (OSError, ValueError, KeyError) as e:  # never leave the screen blank
            print(f"SNAP screen: {e}; retrying in 5 seconds", flush=True)
            time.sleep(5)
