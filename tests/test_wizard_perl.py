"""The setup wizard's Perl side (SnapWizard.pm and snap-wizard.pl --install).

Runs the real Perl with a fake /sys, fake installer commands and a folder
standing in for the installed disk. The window itself needs Gtk3 and WebKit,
which only the Proxmox installer has; the USB end-to-end test covers it.
"""

import json
import os
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

from snaplab.core import config, defaults
from snaplab.stages.snap import answer

WIZARD = Path(__file__).parent.parent / "snaplab" / "delivery" / "usb" / "wizard"
pytestmark = pytest.mark.skipif(
    subprocess.run(["perl", "-MJSON::PP", "-e", "1"], capture_output=True).returncode != 0, reason="no Perl"
)


def perl(code: str, *args: str) -> object:
    """Run Perl with SnapWizard loaded; the code prints one JSON value."""
    out = subprocess.run(
        ["perl", "-I", str(WIZARD), "-MSnapWizard", "-e", code, *args], check=True, capture_output=True, text=True
    )
    return json.loads(out.stdout)


def device(sys_root: Path, kind: str, name: str, bus: str) -> Path:
    target = sys_root / "devices" / bus / name
    target.mkdir(parents=True)
    d = sys_root / kind / name
    d.mkdir(parents=True)
    (d / "device").symlink_to(target)
    return d


def test_disks_skip_usb_the_stick_and_virtual_devices(tmp_path):
    for name, gb, bus in (("nvme0n1", 512, "pci/nvme"), ("sda", 2000, "pci/ata1"), ("sdb", 16, "pci/usb2/2-1")):
        d = device(tmp_path, "block", name, bus)
        (d / "size").write_text(str(gb * 1000000000 // 512))
        (tmp_path / "devices" / bus / name / "model").write_text(f"Model {name}  \n")
    (tmp_path / "block" / "loop0").mkdir()
    (tmp_path / "block" / "loop0" / "size").write_text("100")
    found = perl(
        "print $SnapWizard::JSON->encode(SnapWizard::disks($ARGV[0], $ARGV[1]))", str(tmp_path / "block"), "sda"
    )
    assert found == [{"name": "nvme0n1", "model": "Model nvme0n1", "gb": 512}]


def test_network_ports_and_cables(tmp_path):
    for name, carrier in (("enp1s0", "1"), ("enp2s0", "0")):
        d = device(tmp_path, "class/net", name, "pci/" + name)
        (d / "address").write_text("bc:24:11:00:00:0" + name[3] + "\n")
        (d / "carrier").write_text(carrier + "\n")
    (tmp_path / "class/net/lo").mkdir()
    found = perl("print $SnapWizard::JSON->encode(SnapWizard::interfaces($ARGV[0]))", str(tmp_path / "class/net"))
    assert found == [
        {"name": "enp1s0", "mac": "bc:24:11:00:00:01", "carrier": True},
        {"name": "enp2s0", "mac": "bc:24:11:00:00:02", "carrier": False},
    ]


def checks(cpu_flags: str, mem_gb: int, disks: list, carrier: bool):
    code = "my $in = $SnapWizard::JSON->decode($ARGV[0]);print $SnapWizard::JSON->encode(SnapWizard::checks(%$in))"
    data = {
        "disks": disks,
        "interfaces": [{"name": "enp1s0", "carrier": carrier}],
        "cpuinfo": f"processor\t: 0\nflags\t\t: fpu {cpu_flags} sse2\n",
        "meminfo": f"MemTotal:       {mem_gb * 1024 * 1024} kB\n",
    }
    return {c["level"]: c["text"] for c in perl(code, json.dumps(data))}, perl(code, json.dumps(data))


def test_hardware_check():
    levels, found = checks("vmx", 16, [{"name": "sda", "gb": 512}], True)
    assert [c["level"] for c in found] == ["pass", "pass", "pass", "pass"]
    _, found = checks("", 6, [], False)
    assert [c["level"] for c in found] == ["fix", "fix", "note", "note"]
    assert "Virtualization is turned off" in found[1]["text"] and "6 GB" in found[2]["text"]
    _, found = checks("svm", 2, [{"name": "sda", "gb": 512}], True)
    assert found[2]["level"] == "fix"


def test_progress_lines():
    code = "print $SnapWizard::JSON->encode([SnapWizard::parse_progress($ARGV[0])])"
    assert perl(code, "[2026-10-12T10:00:00Z INFO] progress  42.5 % - extracting base system") == [
        42.5,
        "extracting base system",
    ]
    assert perl(code, "progress 100.0 %") == [100, None]
    assert perl(code, "Installation done.") == []


def test_password_hash_is_sha512_crypt():
    hashed = perl('print $SnapWizard::JSON->encode(SnapWizard::hash_password("correct horse"))')
    salt = hashed.split("$")[2]
    again = subprocess.run(
        ["openssl", "passwd", "-6", "-salt", salt, "correct horse"], check=True, capture_output=True, text=True
    )
    assert again.stdout.strip() == hashed


REQUEST = {
    "host": {
        "hostname": "lab1", "domain": "home.internal", "timezone": "Europe/Berlin", "keyboard": "de",
        "country": "us", "management": {"mode": "dhcp"},
    },
    "networks": defaults.DEFAULTS["networks"],
    "machines": {**defaults.DEFAULTS["machines"], "count": 1},
    "password": "ab3k-9xqe-7tmd-p2wr",
    "disk": "nvme0n1",
}  # fmt: skip


def stick_answer() -> str:
    """The answer.toml the builder puts on a stick built without a disk."""
    cfg, _ = defaults.fill_passwords(config.load())
    return answer.render(cfg)


def test_answer_file_carries_the_wizards_choices():
    code = (
        "my $req = $SnapWizard::JSON->decode($ARGV[1]);"
        "print $SnapWizard::JSON->encode(SnapWizard::answer_toml($ARGV[0], $req, '$6$salt$hash'))"
    )
    text = perl(code, stick_answer(), json.dumps(REQUEST))
    parsed = tomllib.loads(text)
    assert parsed["global"]["fqdn"] == "lab1.home.internal"
    assert parsed["global"]["keyboard"] == "de" and parsed["global"]["timezone"] == "Europe/Berlin"
    assert parsed["global"]["root-password-hashed"] == "$6$salt$hash"
    assert parsed["disk-setup"] == {"filesystem": "ext4", "disk-list": ["nvme0n1"]}
    assert parsed["network"] == {"source": "from-dhcp"}
    assert parsed["first-boot"] == {"source": "from-iso", "ordering": "fully-up"}  # the handoff stays
    static = json.loads(json.dumps(REQUEST))
    static["host"]["management"] = {
        "mode": "static", "address": "192.168.1.50/24", "gateway": "192.168.1.1", "dns": ["192.168.1.1"],
        "iface": "enp1s0", "mac": "BC:24:11:00:00:01",
    }  # fmt: skip
    parsed = tomllib.loads(perl(code, stick_answer(), json.dumps(static)))
    assert parsed["network"] == {
        "source": "from-answer", "cidr": "192.168.1.50/24", "gateway": "192.168.1.1", "dns": "192.168.1.1",
        "filter": {"ID_NET_NAME_MAC": "*bc2411000001"},
    }  # fmt: skip


def test_saved_settings_are_a_valid_snap_yaml():
    code = (
        "my $req = $SnapWizard::JSON->decode($ARGV[0]);"
        "print $SnapWizard::JSON->encode(SnapWizard::wizard_settings($req, '$6$salt$hash'))"
    )
    saved = perl(code, json.dumps(REQUEST))
    assert "management" not in saved["host"] and saved["host"]["root_password_hash"] == "$6$salt$hash"
    assert saved["machines"]["password_hash"] == "$6$salt$hash"
    merged = defaults.merge(config.load(), saved)
    assert config.validate(merged) == []
    assert merged["host"]["hostname"] == "lab1" and merged["machines"]["count"] == 1


def stub(folder: Path, name: str, script: str) -> None:
    path = folder / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_install(tmp_path, installer: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub(bin_dir, "proxmox-low-level-installer", "exit 0\n")
    stub(bin_dir, "proxmox-auto-installer", installer)
    for name in ("vgchange", "mount", "umount", "sync"):
        stub(bin_dir, name, "exit 0\n")
    (tmp_path / "answer.toml").write_text(stick_answer())
    request = tmp_path / "request.json"
    request.write_text(json.dumps(REQUEST))
    target = tmp_path / "target"
    (target / "etc").mkdir(parents=True)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SNAP_ANSWER_TEMPLATE": str(tmp_path / "answer.toml"),
        "SNAP_ANSWERS": str(tmp_path / "answers"),
        "SNAP_TARGET": str(target),
        "SNAP_WIZARD_LOG": str(tmp_path / "wizard.log"),
        "SNAP_WIZARD_CONSOLE": str(tmp_path / "console"),
    }
    out = subprocess.run(
        ["perl", str(WIZARD / "snap-wizard.pl"), "--install", str(request)], env=env, capture_output=True, text=True
    )
    lines = [line.split("\t") for line in out.stdout.splitlines()]
    return lines, target, tmp_path / "answers"


def test_install_runs_the_installer_and_saves_the_lab_settings(tmp_path):
    installer = (
        'grep -q "disk-list = \\[\\"nvme0n1\\"\\]" || { echo "error: wrong answer file"; exit 1; }\n'
        'echo "[INFO] progress  10.0 % - partitioning"\n'
        'echo "[INFO] progress  80.0 % - extracting base system"\n'
        'echo "[INFO] Installation done."\n'
    )
    lines, target, answers = run_install(tmp_path, installer)
    assert lines[-1] == ["DONE"], lines
    progress = [(float(line[1]), line[2]) for line in lines if line[0] == "P"]
    assert (2 + 10 * 0.95, "partitioning") in progress and progress[-1] == (100.0, "Done")
    assert ["L", "[INFO] Installation done."] in lines
    saved = json.loads((target / "etc/snap/wizard.json").read_text())
    assert saved["host"]["hostname"] == "lab1" and saved["host"]["root_password_hash"].startswith("$6$")
    assert (target / "etc/snap/password").read_text() == "ab3k-9xqe-7tmd-p2wr\n"
    assert stat.S_IMODE((target / "etc/snap/wizard.json").stat().st_mode) == 0o600
    assert "nvme0n1" in answers.read_text()
    assert "SNAP wizard: DONE" in (tmp_path / "console").read_text()  # what the end-to-end test waits for


def test_installer_failure_is_reported(tmp_path):
    lines, target, _ = run_install(tmp_path, 'echo "[ERROR] Installation failed: disk too small"\nexit 1\n')
    assert lines[-1] == ["FAIL", "[ERROR] Installation failed: disk too small"]
    assert not (target / "etc/snap/wizard.json").exists()
