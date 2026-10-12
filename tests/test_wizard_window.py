"""The setup wizard's real window (snap-wizard.pl with Gtk3 and WebKit) on a virtual screen.

Runs the window the way the Proxmox installer does, in its hands-off mode
(kernel option snap.wizard=auto), with a stand-in computer (/sys, /proc) and
stand-in installer commands: the page loads, asks the window about the
computer, starts the install, shows its progress, and the window closes when
it is done. Needs Xvfb and the Perl Gtk3 WebKit bindings (Debian/Ubuntu:
xvfb libgtk3-webkit2-perl); skipped without them.
"""

import json
import os
import shutil
import subprocess

import pytest
from test_wizard_perl import WIZARD, device, stick_answer, stub

from snaplab.core import config
from snaplab.delivery.usb import wizard_files

pytestmark = pytest.mark.skipif(
    shutil.which("xvfb-run") is None
    or subprocess.run(["perl", "-MGtk3::WebKit2", "-e", "1"], capture_output=True).returncode != 0,
    reason="needs xvfb-run and the Perl Gtk3 WebKit bindings",
)


def computer(tmp_path):
    """A computer with one NVMe disk and one network port, booted from the stick (sdb)."""
    sys_root = tmp_path / "sys"
    for name, gb, bus in (("nvme0n1", 512, "pci/nvme"), ("sdb", 16, "pci/usb2/2-1")):
        d = device(sys_root, "block", name, bus)
        (d / "size").write_text(str(gb * 1000000000 // 512))
        (sys_root / "devices" / bus / name / "model").write_text(f"Model {name}\n")
    nic = device(sys_root, "class/net", "enp1s0", "pci/enp1s0")
    (nic / "address").write_text("bc:24:11:00:00:01\n")
    (nic / "carrier").write_text("1\n")
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "cmdline").write_text("ro quiet snap.wizard=auto\n")
    (proc / "cpuinfo").write_text("processor\t: 0\nflags\t\t: fpu vmx sse2\n")
    (proc / "meminfo").write_text(f"MemTotal:       {16 * 1024 * 1024} kB\n")
    (proc / "mounts").write_text("/dev/sdb /cdrom iso9660 ro 0 0\n")
    return sys_root, proc


def test_window_installs_by_itself_and_closes(tmp_path):
    site = tmp_path / "wizard"
    site.mkdir()
    for name, content in wizard_files.files(config.load()).items():
        (site / name).write_bytes(content)
    (site / "snap-wizard.pl").write_bytes((WIZARD / "snap-wizard.pl").read_bytes())
    sys_root, proc = computer(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub(bin_dir, "proxmox-low-level-installer", "exit 0\n")
    stub(bin_dir, "proxmox-auto-installer", 'cat >/dev/null\necho "progress  50.0 % - extracting"\nsleep 1\n')
    for name in ("vgchange", "mount", "umount", "sync"):
        stub(bin_dir, name, "exit 0\n")
    (tmp_path / "answer.toml").write_text(stick_answer())
    target = tmp_path / "target"
    (target / "etc").mkdir(parents=True)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SNAP_SYS": str(sys_root),
        "SNAP_PROC": str(proc),
        "SNAP_ANSWER_TEMPLATE": str(tmp_path / "answer.toml"),
        "SNAP_ANSWERS": str(tmp_path / "answers"),
        "SNAP_TARGET": str(target),
        "SNAP_WIZARD_LOG": str(tmp_path / "wizard.log"),
        "SNAP_WIZARD_CONSOLE": str(tmp_path / "console"),
    }
    result = subprocess.run(
        ["xvfb-run", "-a", "-s", "-screen 0 1024x768x24", "perl", str(site / "snap-wizard.pl")],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    console = (tmp_path / "console").read_text() if (tmp_path / "console").exists() else ""
    assert result.returncode == 0, result.stderr + console
    assert "SNAP wizard: action ready" in console and "SNAP wizard: action install" in console
    assert "SNAP wizard: P 49.5 extracting" in console  # the installer's 50 %, on the wizard's scale
    assert console.rstrip().endswith("SNAP wizard: DONE")
    assert 'disk-list = ["nvme0n1"]' in (tmp_path / "answers").read_text()  # never the stick
    saved = json.loads((target / "etc/snap/wizard.json").read_text())
    assert saved["host"]["hostname"] == "snap" and saved["machines"]["count"] == 2
