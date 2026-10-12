"""BANG: the last check, the summary, the clean-up, and setup no longer starting by itself.

Runs CRACKLE, POP and BANG one after the other on the fake Proxmox host, as
`snap resume` does on a real one.
"""

import json

import pytest
from fake_proxmox import FakeProxmox
from test_crackle import stock_payload
from test_pop import cfg, host  # noqa: F401 - fixtures

from snaplab import screen
from snaplab.core.engine import LOG_FILE, PROGRESS_DIR, Context, Engine
from snaplab.stages.bang import stage as bang
from snaplab.stages.crackle import tools as crackle_tools
from snaplab.stages.crackle.stage import STAGE as CRACKLE
from snaplab.stages.pop import lab
from snaplab.stages.pop import stage as pop

PASSWORD = "ab3k-9xqe-7tmd-p2wr"


class SetupService:
    """systemctl for the snap-resume service, on top of the fake host's commands."""

    def __init__(self, fake):
        self.enabled = True
        original = fake._dispatch

        def dispatch(cmd):
            if cmd[:2] == ("systemctl", "is-enabled"):
                return "" if self.enabled else None
            if cmd[:2] == ("systemctl", "disable") and cmd[-1] == bang.RESUME_SERVICE:
                self.enabled = False
                return ""
            if cmd[:3] == ("ip", "-j", "-4") and cmd[3:] == ("addr", "show", "vmbr0"):
                local = {"family": "inet", "local": "192.168.1.50"}
                return json.dumps([{"ifname": "vmbr0", "addr_info": [local]}])
            if cmd[0] == crackle_tools.TOFU and not cmd[1].startswith("-chdir="):
                return FakeProxmox._dispatch(fake, cmd)  # CRACKLE's version check
            return original(cmd)

        fake._dispatch = dispatch


@pytest.fixture
def lab_host(host):  # noqa: F811 - the POP fixture, ready for CRACKLE too
    stock_payload(host)
    host.service = SetupService(host)
    host.write(bang.PASSWORD_FILE, PASSWORD + "\n", mode=0o600)
    return host


def engine(fake, messages=None):
    say = messages.append if messages is not None else (lambda _: None)
    return Engine([CRACKLE, pop.STAGE, bang.STAGE], Context(host=fake, cfg=fake.cfg, node="snap", say=say))


def test_bang_finishes_the_lab(lab_host):
    messages = []
    assert engine(lab_host, messages).resume(), messages
    assert lab_host.exists(f"{PROGRESS_DIR}/bang.done")
    assert "SNAP: BANG (final checks) done" in messages
    # The check from inside the lab ran again, after POP's own.
    assert lab_host.playbooks == ["router.yml", "verify.yml", "verify.yml"]
    # The summary, for the ready screen and the user.
    s = json.loads(lab_host.read(bang.SUMMARY_JSON))
    assert s["web"] == "https://192.168.1.50:8006" and s["user"] == "root" and s["router"] == "snap-router"
    assert [n["name"] for n in s["networks"]] == ["lab", "services"]
    assert [m["name"] for m in s["machines"]] == ["ubuntu-01", "ubuntu-02", "ubuntu-03"]
    text = lab_host.read(bang.SUMMARY_TEXT)
    assert "Open in a browser:  https://192.168.1.50:8006" in text and "ubuntu-01  10.10.10.100" in text
    assert PASSWORD not in text  # the summary never holds the password
    # Clean-up: Ansible's copy of the router key is gone; what is kept is root-only.
    assert not lab_host.exists(f"{pop.ANSIBLE_DIR}/api.json")
    assert lab_host.exists(pop.API_FILE)  # kept to change the lab later
    assert lab_host.path(pop.CONFIG_ISO).stat().st_mode & 0o777 == 0o600
    # Setup no longer starts by itself.
    assert lab_host.service.enabled is False


def test_keys_and_passwords_are_removed_from_the_logs(lab_host):
    # A command that printed the router key and the password into the logs.
    key = None

    def leak(ctx):
        nonlocal key
        key = json.loads(ctx.host.read(pop.API_FILE))["secret"]
        ctx.host.append(LOG_FILE, f"ansible output: secret={key} password={PASSWORD}\n")
        ctx.host.append(pop.CONSOLE_LOG, f"login with {PASSWORD}\n")
        return True

    assert engine(lab_host).resume()
    leak(Context(host=lab_host, cfg=lab_host.cfg, node="snap"))
    lab_host.path(f"{PROGRESS_DIR}/bang.done").unlink()
    assert not bang.cleaned(Context(host=lab_host, cfg=lab_host.cfg, node="snap"))
    assert engine(lab_host).resume()
    log = lab_host.read(LOG_FILE)
    assert key not in log and PASSWORD not in log and f"secret={bang.REDACTED}" in log
    assert PASSWORD not in lab_host.read(pop.CONSOLE_LOG)
    assert lab_host.read(bang.PASSWORD_FILE).strip() == PASSWORD  # the ready screen still shows it


def test_a_stopped_machine_is_started(lab_host):
    assert engine(lab_host).resume()
    lab_host.running.discard(102)
    for path in (f"{PROGRESS_DIR}/bang.done", bang.CHECKED):
        lab_host.path(path).unlink()
    messages = []
    assert engine(lab_host, messages).resume()
    assert 102 in lab_host.running and "ubuntu-02 was not running; starting it" in messages


def test_a_broken_earlier_step_stops_bang(lab_host):
    assert engine(lab_host).resume()
    for path in (f"{PROGRESS_DIR}/bang.done", bang.CHECKED, f"{pop.WORK}/verified.sha256"):
        lab_host.path(path).unlink()
    lab_host.vms.pop(lab.FIRST_MACHINE_VMID)  # a lab machine was deleted by hand
    messages = []
    # POP is done, so only BANG runs, and it says what no longer holds rather than covering it up.
    assert not engine(lab_host, messages).resume()
    failure = next(m for m in messages if "BANG (final checks) failed" in m)
    assert "Create the lab machines with OpenTofu" in failure


def test_second_run_changes_nothing(lab_host):
    assert engine(lab_host).resume()
    lab_host.path(f"{PROGRESS_DIR}/bang.done").unlink()
    lab_host.playbooks.clear()
    messages = []
    assert engine(lab_host, messages).resume()
    assert sum("already done" in m for m in messages) == len(bang.STAGE.steps)
    assert lab_host.playbooks == []


def test_ready_screen_waits_for_bang(lab_host):
    lab_host.write(f"{PROGRESS_DIR}/snap.done", "1\n")  # written by the handoff
    eng = engine(lab_host)
    assert eng.resume()
    assert screen.snapshot(eng)["phase"] == "ready"
    lab_host.path(f"{PROGRESS_DIR}/bang.done").unlink()
    assert screen.snapshot(eng)["phase"] == "working"
