"""The lab computer's own screen (snaplab/screen.py): setting up, lab ready, needs attention."""

import io
import json
import os
import re

import pytest

from snaplab import hostcli, screen
from snaplab.core import defaults
from snaplab.core.engine import PROGRESS_DIR, STATE_FILE, Context, Engine
from snaplab.core.host import CommandError, Host
from snaplab.stages.pop import addresses

ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


class LabComputer(Host):
    """A folder for the disk, and `ip` answering with the computer's address."""

    def __init__(self, root, address="192.168.1.50"):
        super().__init__(root)
        self.address, self.commands = address, []

    def run(self, *cmd, input=None):
        self.commands.append(cmd)
        if cmd[:2] == ("ip", "-j") and self.address:
            return json.dumps([{"ifname": "vmbr0", "addr_info": [{"family": "inet", "local": self.address}]}])
        raise CommandError(list(cmd), 1, "no such device")


@pytest.fixture
def computer(tmp_path):
    return LabComputer(tmp_path)


def engine_for(host, mode="permanent"):
    cfg = json.loads(json.dumps(defaults.DEFAULTS))
    cfg["host"]["mode"] = mode
    return Engine(hostcli.STAGES, Context(host=host, cfg=cfg, node="snap", say=lambda _: None))


def finished(host, *stages):
    for name in stages:
        host.write(f"{PROGRESS_DIR}/{name}.done", "1\n")


def record(host, stage, status, steps, error=None):
    state = json.loads(host.read(STATE_FILE) or '{"stages": {}}')
    entry = {"status": status, "steps": {name: {"status": s} for name, s in steps.items()}}
    if error:
        entry["error"] = error
    state["stages"][stage] = entry
    host.write(STATE_FILE, json.dumps(state))


def text(host, mode="permanent", **kw):
    snap = screen.snapshot(engine_for(host, mode))
    return "\n".join(ANSI.sub("", line) for line in screen.render(snap, **kw))


def test_setting_up_shows_each_stage_and_the_current_step(computer):
    finished(computer, "snap")
    record(
        computer, "crackle", "running", {"proxmox-ready": "done", "package-sources": "done", "lab-bridge": "running"}
    )
    out = text(computer)
    assert "Setting up your lab. Keep this computer on (about 65 minutes left)." in out
    assert "[x] SNAP     Install Proxmox" in out
    assert "[>] CRACKLE  Prepare this computer" in out
    assert "[ ] POP" in out and "[ ] BANG" in out
    assert "CRACKLE, step 3 of 9:  Create the internal lab bridge vmbr1" in out
    assert "]  2 / 9" in out
    assert "Web page when finished: https://192.168.1.50:8006" in out
    assert "Password:" not in out  # only after pressing P
    assert "[L] Show log   [P] Show password   [O] Turn off" in out


def test_lab_ready_shows_how_to_log_in_and_what_was_built(computer):
    finished(computer, "snap", "crackle", "pop", "bang")
    computer.write(screen.PASSWORD_FILE, "ab3k-9xqe-7tmd-p2wr\n")
    out = text(computer)
    assert "Your lab is ready.  You can remove the USB stick." in out
    assert "Open in your browser:   https://192.168.1.50:8006" in out
    assert "User name:              root" in out and "Password:               ab3k-9xqe-7tmd-p2wr" in out
    assert "lab        10.10.10.0/24    internet only" in out
    assert "services   10.10.20.0/24    isolated" in out
    assert "alpine-01  10.10.10.100     user labuser" in out and "alpine-02" in out
    assert "choose Advanced, then Continue" in out


def test_volatile_lab_says_it_is_gone_when_turned_off(computer):
    finished(computer, "snap")
    working = text(computer, mode="volatile")
    assert "[x] SNAP     Start Proxmox from the USB stick" in working
    assert "Keep this computer on and the USB stick in" in working
    finished(computer, "crackle", "pop", "bang")
    ready = text(computer, mode="volatile")
    assert "Your lab is ready." in ready and "You can remove the USB stick" not in ready
    assert "Volatile lab: everything is lost when you turn off this computer or remove the stick." in ready
    s = screen.Screen(engine_for(computer, "volatile"), out=io.StringIO(), keys_fd=os.pipe()[0], run=lambda _: None)
    s.size = lambda: (100, 30)
    s.key("o")
    assert "This deletes the lab. Turn off now? Press Y for yes" in ANSI.sub("", s.frame()[-1])


def test_ready_screen_shows_addresses_pop_moved(computer):
    """If the home network already used 10.10.10.0/24, the screen shows where the lab went."""
    finished(computer, "snap", "crackle", "pop", "bang")
    placed = {"networks": {"lab": "10.10.30.0/24", "services": "10.10.20.0/24"}, "admin": "172.31.255.0/29"}
    computer.write(addresses.PLAN_FILE, json.dumps({"for": [], "busy": [], "placed": placed}))
    out = text(computer)
    assert "lab        10.10.30.0/24" in out and "alpine-01  10.10.30.100" in out


def test_no_password_file_points_to_the_saved_one(computer):
    finished(computer, "snap", "crackle", "pop", "bang")
    assert "the one from your setup (or snap.img.password.txt)" in text(computer)


def test_failure_says_what_happened_and_what_to_do(computer):
    finished(computer, "snap", "crackle")
    steps = {"addresses": "done", "opnsense-template": "done", "admin-network": "done", "ssh-key": "done"}
    record(
        computer,
        "pop",
        "failed",
        {**steps, "router-config": "done", "router": "failed"},
        error="Create the router and import its config: the router did not answer within 15 minutes",
    )
    out = text(computer)
    assert "POP needs attention.  Nothing was lost; SNAP stopped safely." in out
    assert "[!] POP      Stopped at step 6 of 9: Create the router and import its config" in out
    assert "the router did not answer within 15 minutes" in out
    assert "[R] Try again   [L] Show log   [O] Turn off" in out


def test_logo_only_when_the_screen_is_tall_enough(computer):
    finished(computer, "snap")
    assert "|||||||" not in text(computer, height=25)
    assert "|||||||" in text(computer, height=50)


def test_screen_fits_a_small_monitor(computer):
    finished(computer, "snap", "crackle", "pop", "bang")
    lines = screen.render(screen.snapshot(engine_for(computer)), 80, 25, colour=False)
    assert len(lines) <= 25 and max(len(line) for line in lines) <= 80


def test_without_an_address_yet(computer):
    computer.address = None
    finished(computer, "snap", "crackle", "pop", "bang")
    assert "https://<this computer>:8006" in text(computer)


def keys_screen(computer):
    ran = []
    r, w = os.pipe()
    s = screen.Screen(engine_for(computer), out=io.StringIO(), keys_fd=r, run=ran.append)
    s.size = lambda: (80, 25)
    return s, ran, w


def test_keys(computer):
    finished(computer, "snap", "crackle")
    record(computer, "pop", "failed", {"addresses": "failed"}, error="Choose lab addresses: no free range")
    s, ran, w = keys_screen(computer)
    assert s.key("r") and ran == ["systemctl restart --no-block snap-resume.service"]
    assert s.key("p") and s.show_password
    assert s.key("o") and s.confirm_off and "Press Y for yes" in s.frame()[-1]
    assert s.key("n") and not s.confirm_off and ran == ["systemctl restart --no-block snap-resume.service"]
    s.key("o")
    assert not s.key("Y") and ran[-1] == "systemctl poweroff"


def test_try_again_only_after_a_failure(computer):
    finished(computer, "snap")
    s, ran, _ = keys_screen(computer)
    s.key("r")
    assert ran == []


def test_show_log(computer):
    computer.write("/var/log/snap/engine.log", "".join(f"line {i}\n" for i in range(100)))
    s, _, w = keys_screen(computer)
    os.write(w, b"x")
    s.show_log()
    shown = s.out.getvalue()
    assert "line 99" in shown and "line 10\n" not in shown and "Press any key to go back" in shown


def test_firstboot_installs_the_screen(tmp_path):
    from pathlib import Path

    unit = Path(__file__).parent.parent / "snaplab/stages/snap/firstboot.sh"
    body = unit.read_text()
    assert "ExecStart=/var/lib/snap/bin/snap screen" in body and "TTYPath=/dev/tty1" in body
    assert "enable --now --no-block snap-resume.service snap-screen.service" in body


def test_progress_lines_stay_off_the_monitor_while_the_screen_shows(monkeypatch, tmp_path):
    assert hostcli.consoles(False) == ["/dev/console"]
    active = tmp_path / "active"
    active.write_text("ttyS0 tty0\n")
    real_open = open
    monkeypatch.setattr(
        "builtins.open", lambda p, *a, **k: real_open(active if p == "/sys/class/tty/console/active" else p, *a, **k)
    )
    assert hostcli.consoles(True) == ["/dev/ttyS0"]  # serial yes, the monitor (tty0) no
