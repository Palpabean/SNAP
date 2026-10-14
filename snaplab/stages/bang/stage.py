"""BANG (stage 4, on the host): the last checks, the summary and the clean-up (Design 0001, section 7.4).

1. Check the whole lab once more: every CRACKLE and POP step still holds, the
   router and the lab machines are running, and the check from inside the lab
   passes again.
2. Write the summary: the web address, login, networks and machines, for the
   ready screen and as /root/snap-lab.txt.
3. Remove spare copies of keys and passwords: the copy of the router's API key
   POP left for Ansible, and any key or password in the logs.
   The router's bootstrap config disk (it holds the API key) becomes readable
   by root only. Kept on purpose, root-only: the OpenTofu API token, the
   router's API key and the SSH key (to change the lab later, D7), and the
   setup wizard's password file, which the ready screen shows.
4. Stop the setup service from running at every start-up.

Standard library only (runs on the Proxmox host).
"""

from __future__ import annotations

import json
from typing import Any

from snaplab.core.engine import LOG_FILE, Context, Stage, Step
from snaplab.core.host import CommandError
from snaplab.stages.crackle import tools as crackle_tools
from snaplab.stages.crackle.stage import STAGE as CRACKLE
from snaplab.stages.pop import lab
from snaplab.stages.pop import stage as pop

WORK = "/var/lib/snap/bang"
CHECKED = f"{WORK}/checked"
SUMMARY_JSON = "/var/lib/snap/summary.json"
SUMMARY_TEXT = "/root/snap-lab.txt"
PASSWORD_FILE = "/etc/snap/password"  # written by the setup wizard on the stick
RESUME_SERVICE = "snap-resume.service"
WEB_PORT = 8006
REDACTED = "[removed by SNAP]"
# Logs that may have caught a key or password in a command's output. (The state
# file is the engine's own and is saved after every step, so it is left alone;
# it holds only step names, times and short error lines.)
SCRUBBED = [LOG_FILE, pop.CONSOLE_LOG, "/var/log/snap/firstboot.log"]
POLICIES = {"internet-only": "internet only", "isolated": "isolated", "open": "internet and other lab networks"}


# --- 1. the last check ------------------------------------------------------------


def _lab_vms(ctx: Context) -> list[tuple[int, str]]:
    cfg = pop._cfg(ctx)
    return [(lab.ROUTER_VMID, lab.ROUTER_NAME)] + [(m["vmid"], m["name"]) for m in lab.machines(cfg)]


def checked(ctx: Context) -> bool:
    return ctx.host.exists(CHECKED)


def check(ctx: Context) -> None:
    broken = [f"{stage.title}: {s.title}" for stage in (CRACKLE, pop.STAGE) for s in stage.steps if not s.done(ctx)]
    if broken:
        raise RuntimeError("these earlier steps no longer hold: " + "; ".join(broken))
    for vmid, name in _lab_vms(ctx):
        if "running" not in ctx.host.run("qm", "status", str(vmid)):
            ctx.say(f"{name} was not running; starting it")
            ctx.host.run("qm", "start", str(vmid))
    if not pop.router_ready(ctx):
        pop.wait_for_router(ctx)
    if lab.machines(pop._cfg(ctx)):
        pop.verify(ctx)  # the check from inside the lab, run again now
    ctx.host.write(CHECKED, "ok\n")


# --- 2. the summary -------------------------------------------------------------


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


def summary(ctx: Context) -> dict[str, Any]:
    cfg = pop._cfg(ctx)
    host_cfg = cfg.get("host", {})
    return {
        "web": web_address(ctx.host),
        "user": "root",
        "name": f"{ctx.node}.{host_cfg['domain']}" if host_cfg.get("domain") else ctx.node,
        "router": lab.ROUTER_NAME,
        "mode": host_cfg.get("mode", "permanent"),
        "networks": [
            {"name": n["name"], "cidr": n["cidr"], "policy": POLICIES.get(n.get("policy", ""), n.get("policy", ""))}
            for n in cfg["networks"]
        ],
        "machines": [
            {"name": m["name"], "ip": m["ip"], "user": cfg["machines"]["username"]} for m in lab.machines(cfg)
        ],
    }


def summary_text(s: dict[str, Any]) -> str:
    lines = [
        "SNAP: your lab",
        "",
        f"Open in a browser:  {s['web'] or 'https://<this computer>:8006'}",
        f"User name:          {s['user']}",
        "Password:           the one from your setup (or snap.img.password.txt next to the stick image)",
        "",
        f"Router:             {s['router']}",
    ]
    for i, n in enumerate(s["networks"]):
        lines.append(f"{'Networks:' if i == 0 else '':<20}{n['name']:<10} {n['cidr']:<16} {n['policy']}")
    for i, m in enumerate(s["machines"]):
        lines.append(f"{'Machines:' if i == 0 else '':<20}{m['name']:<10} {m['ip']:<16} user {m['user']}")
    if s.get("mode") == "volatile":
        lines += ["", "This is a volatile lab: it runs from the USB stick and this computer's memory, and is",
                  "gone, with everything in it, when the computer is turned off or the stick is removed."]  # fmt: skip
    lines += [
        "",
        "To change the lab later: edit the settings in /var/lib/snap/pop and apply them again",
        "(see the SNAP design document, section 7.3). `snap status` lists every step.",
    ]
    return "\n".join(lines) + "\n"


def summary_done(ctx: Context) -> bool:
    text = ctx.host.read(SUMMARY_JSON)
    return bool(text) and json.loads(text) == summary(ctx) and ctx.host.exists(SUMMARY_TEXT)


def write_summary(ctx: Context) -> None:
    s = summary(ctx)
    ctx.host.write(SUMMARY_JSON, json.dumps(s, indent=2) + "\n")
    ctx.host.write(SUMMARY_TEXT, summary_text(s), mode=0o600)


# --- 3. spare copies of keys and passwords ---------------------------------------


def secrets(ctx: Context) -> list[str]:
    """The keys and passwords on this computer, to look for where they should not be."""
    found = []
    api = ctx.host.read(pop.API_FILE)
    if api:
        found += [v for k, v in json.loads(api).items() if k in ("key", "secret")]
    token = (ctx.host.read(crackle_tools.API_SECRET) or "").strip()
    if "=" in token:
        found.append(token.split("=", 1)[1])
    password = (ctx.host.read(PASSWORD_FILE) or "").strip()
    if password:
        found.append(password)
    return [s for s in found if len(s) >= 8]  # never blank out short, common strings


def _spare_copies(ctx: Context) -> list[str]:
    left = []
    if ctx.host.exists(f"{pop.ANSIBLE_DIR}/api.json"):
        left.append(f"{pop.ANSIBLE_DIR}/api.json")
    iso = ctx.host.path(pop.CONFIG_ISO)
    if iso.exists() and iso.stat().st_mode & 0o077:
        left.append(pop.CONFIG_ISO)
    keys = secrets(ctx)
    left += [f for f in SCRUBBED if any(k in (ctx.host.read(f) or "") for k in keys)]
    return left


def cleaned(ctx: Context) -> bool:
    return not _spare_copies(ctx)


def clean(ctx: Context) -> None:
    ctx.host.remove(f"{pop.ANSIBLE_DIR}/api.json")  # Ansible's copy; POP writes it again when it needs it
    if ctx.host.exists(pop.CONFIG_ISO):
        ctx.host.path(pop.CONFIG_ISO).chmod(0o600)
    keys = secrets(ctx)
    for path in SCRUBBED:
        text = ctx.host.read(path)
        if text and any(k in text for k in keys):
            for k in keys:
                text = text.replace(k, REDACTED)
            ctx.host.write(path, text, mode=0o600)


# --- 4. start-up -----------------------------------------------------------------


def autostart_off(ctx: Context) -> bool:
    return not ctx.host.ok("systemctl", "is-enabled", "--quiet", RESUME_SERVICE)


def stop_autostart(ctx: Context) -> None:
    # Not --now: this runs inside that service. Its unit also stops once bang.done exists.
    ctx.host.run("systemctl", "disable", RESUME_SERVICE)


STAGE = Stage(
    name="bang",
    title="BANG (final checks)",
    steps=[
        Step("final-check", "Check the whole lab one last time", checked, check),
        Step("summary", "Write the lab summary", summary_done, write_summary),
        Step("clean-up", "Remove spare copies of keys and passwords", cleaned, clean),
        Step("autostart", "Stop running setup at every start-up", autostart_off, stop_autostart),
    ],
)
