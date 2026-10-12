"""The disk prompt (snaplab/delivery/usb/choose-disk.sh) against a fake /sys/block.

It runs in busybox's shell in the installer; bash runs the same script here.
"""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "snaplab" / "delivery" / "usb" / "choose-disk.sh"
ANSWER = '[disk-setup]\nfilesystem = "ext4"\ndisk-list = ["snap-choose-at-boot"]\n'
# Stand-ins for the installer's own functions and commands.
STUBS = """
debugsh_err_reboot() { echo "STOPPED: $1"; exit 3; }
poweroff() { echo "POWEROFF"; exit 4; }
sleep() { :; }
"""


def disk(sys_block: Path, name: str, gb: int, model: str, bus: str = "pci0000:00/0000:00:17.0/ata1"):
    device = sys_block.parent / "devices" / bus / name
    device.mkdir(parents=True)
    (device / "model").write_text(model + "   \n")
    d = sys_block / name
    d.mkdir(parents=True)
    (d / "size").write_text(str(gb * 1000000000 // 512))
    (d / "device").symlink_to(device)


@pytest.fixture
def machine(tmp_path):
    sys_block = tmp_path / "sys" / "block"
    sys_block.mkdir(parents=True)
    root = tmp_path / "installer"
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / "proxmox-fetch-answer").write_text("#!/bin/sh\noriginal\n")
    answer = tmp_path / "answer.toml"
    answer.write_text(ANSWER)
    # The stick (sdb on USB) and a few devices that are never disks to install on.
    disk(sys_block, "sdb", 16, "SanDisk Ultra", bus="pci0000:00/0000:00:14.0/usb2/2-1")
    for name in ("loop0", "sr0", "zram0"):
        (sys_block / name).mkdir()
        (sys_block / name / "size").write_text("1000")
    return sys_block, root, answer


def run(machine, keys: str = ""):
    sys_block, root, answer = machine
    env = {
        "PATH": "/usr/bin:/bin",
        "cdrom": "/dev/sdb",
        "SNAP_SYS_BLOCK": str(sys_block),
        "SNAP_INSTALLER_ROOT": str(root),
        "SNAP_ANSWER": str(answer),
    }
    proc = subprocess.run(
        ["bash", "-c", STUBS + f'. "{SCRIPT}"'], input=keys, capture_output=True, text=True, env=env, timeout=30
    )
    chosen = root / "etc" / "snap" / "answer.toml"
    return proc, chosen.read_text() if chosen.exists() else None


def test_single_disk_is_used_after_the_countdown(machine):
    disk(machine[0], "nvme0n1", 512, "Samsung SSD 980", bus="pci0000:00/0000:00:1d.0/nvme/nvme0")
    proc, answer = run(machine)  # no key pressed: the countdown runs out
    assert "SNAP will install Proxmox VE on: nvme0n1  Samsung SSD 980  512 GB" in proc.stdout
    assert answer == ANSWER.replace("snap-choose-at-boot", "nvme0n1")
    fetch = machine[1] / "usr" / "bin" / "proxmox-fetch-answer"
    assert "exec cat /etc/snap/answer.toml" in fetch.read_text()
    assert (fetch.parent / "proxmox-fetch-answer.orig").read_text() == "#!/bin/sh\noriginal\n"


def test_several_disks_ask_and_need_confirmation(machine):
    disk(machine[0], "sda", 2000, "WDC WD20EZRZ")
    disk(machine[0], "nvme0n1", 512, "Samsung SSD 980", bus="pci0000:00/0000:00:1d.0/nvme/nvme0")
    # Pick 5 (no such disk), then 2 without confirming, then 2 confirmed.
    proc, answer = run(machine, "5\n2\nyes\n2\nERASE\n")
    assert "1) nvme0n1  Samsung SSD 980  512 GB" in proc.stdout
    assert "2) sda  WDC WD20EZRZ  2000 GB" in proc.stdout
    assert "Please type one of the numbers shown." in proc.stdout
    assert "Not confirmed; nothing was changed." in proc.stdout
    assert answer == ANSWER.replace("snap-choose-at-boot", "sda")


def test_enter_during_the_countdown_opens_the_menu(machine):
    disk(machine[0], "sda", 256, "Crucial MX500")
    proc, answer = run(machine, "\n0\n")
    assert proc.returncode == 4 and "POWEROFF" in proc.stdout
    assert answer is None  # nothing chosen, nothing written


def test_no_internal_disk_stops(machine):
    proc, answer = run(machine)
    assert proc.returncode == 3 and "found no internal disk" in proc.stdout
    assert answer is None


def test_stick_and_usb_drives_are_never_offered(machine):
    disk(machine[0], "sdc", 1000, "External HDD", bus="pci0000:00/0000:00:14.0/usb3/3-2")
    disk(machine[0], "sda", 256, "Crucial MX500")
    proc, answer = run(machine)
    assert "sdb" not in proc.stdout and "sdc" not in proc.stdout
    assert answer == ANSWER.replace("snap-choose-at-boot", "sda")


def test_nothing_happens_when_the_stick_names_a_disk(machine):
    machine[2].write_text('[disk-setup]\nfilter.ID_SERIAL_SHORT = "S4EW"\n')
    disk(machine[0], "sda", 256, "Crucial MX500")
    proc, answer = run(machine)
    assert proc.returncode == 0 and proc.stdout == "" and answer is None
