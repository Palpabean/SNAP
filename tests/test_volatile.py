"""The volatile lab: its ready-made Proxmox system's SNAP pieces, and its setup at each start."""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess

import pytest

from snaplab.core import defaults, logo
from snaplab.delivery.usb import menu, volatile

SCRIPT = volatile.resources.files("snaplab.delivery.usb").joinpath("live", "snap-volatile")


def load_script():
    loader = importlib.machinery.SourceFileLoader("snap_volatile", str(SCRIPT))
    spec = importlib.util.spec_from_loader("snap_volatile", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def sv(tmp_path, monkeypatch):
    """snap-volatile against a stand-in computer: a root folder, a stick and /sys/class/net."""
    stick, root = tmp_path / "stick", tmp_path / "root"
    stick.mkdir()
    (root / "etc/network").mkdir(parents=True)
    cfg = json.loads(json.dumps(defaults.DEFAULTS))
    cfg["host"]["mode"] = "volatile"
    (stick / "snap.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("SNAP_STICK", str(stick))
    monkeypatch.setenv("SNAP_ROOT", str(root))
    monkeypatch.setenv("SNAP_DRY_RUN", "1")
    module = load_script()
    module.cfg_path = stick / "snap.json"
    return module, root, stick


def nic(sys_net, name, carrier=False, mac="bc:24:11:00:00:01", wireless=False, kind="1"):
    d = sys_net / name
    (d / "device").mkdir(parents=True)
    (d / "carrier").write_text("1\n" if carrier else "0\n")
    (d / "address").write_text(mac + "\n")
    (d / "type").write_text(kind + "\n")
    if wireless:
        (d / "wireless").mkdir()


def test_ports_are_wired_ports_only(sv, tmp_path):
    module, _, _ = sv
    net = tmp_path / "net"
    nic(net, "enp2s0")
    nic(net, "enp1s0")
    nic(net, "wlp3s0", wireless=True)
    (net / "lo").mkdir()
    (net / "bonding_masters").parent.mkdir(exist_ok=True)
    (net / "bonding_masters").write_text("")  # a file, not a port: what tripped the spike
    nic(net, "vmbr0")
    (net / "vmbr0" / "bridge").mkdir()
    assert module.ports(net) == ["enp1s0", "enp2s0"]


def test_port_with_a_cable_wins(sv, tmp_path):
    module, _, _ = sv
    net = tmp_path / "net"
    nic(net, "enp1s0")
    nic(net, "enp2s0", carrier=True)
    assert module.pick_port({"mode": "dhcp"}, net, wait=0) == "enp2s0"
    (net / "enp2s0" / "carrier").write_text("0\n")
    assert module.pick_port({"mode": "dhcp"}, net, wait=0) == "enp1s0"  # none: the first
    nic(net, "enp3s0", mac="bc:24:11:00:00:03")
    assert module.pick_port({"mode": "static", "mac": "BC:24:11:00:00:03"}, net, wait=0) == "enp3s0"


def test_interfaces_put_the_port_in_the_main_bridge(sv):
    module, _, _ = sv
    dhcp = module.interfaces("enp1s0", {"mode": "dhcp"})
    assert "iface enp1s0 inet manual" in dhcp and "auto vmbr0\niface vmbr0 inet dhcp" in dhcp
    assert "\tbridge-ports enp1s0" in dhcp
    static = module.interfaces("enp1s0", {"mode": "static", "address": "192.168.1.50/24", "gateway": "192.168.1.1"})
    assert "iface vmbr0 inet static\n\taddress 192.168.1.50/24\n\tgateway 192.168.1.1" in static


def test_early_sets_up_this_computer(sv, tmp_path, monkeypatch):
    module, root, stick = sv
    cfg = json.loads((stick / "snap.json").read_text())
    cfg["host"]["root_password_hash"] = "$6$salt$hash"
    (stick / "snap.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(module, "pick_port", lambda mgmt: "enp1s0")
    module.early()
    assert (root / "etc/hostname").read_text() == "snap\n"
    assert "iface vmbr0 inet dhcp" in (root / "etc/network/interfaces").read_text()
    assert "Connecting to the network through enp1s0" in (root / "run/snap-volatile/status").read_text()


def test_hosts_maps_the_name_to_the_network_address(sv, monkeypatch):
    module, root, _ = sv
    monkeypatch.setattr(module, "address", lambda: "192.168.1.77")
    module.hosts()
    assert "192.168.1.77 snap.lab.internal snap" in (root / "etc/hosts").read_text()
    assert "https://192.168.1.77:8006" in (root / "run/snap-volatile/status").read_text()


def test_without_dhcp_the_lab_still_starts(sv, monkeypatch):
    module, root, _ = sv
    monkeypatch.setattr(module, "address", lambda: None)
    module.hosts()
    assert "192.168.100.10 snap.lab.internal snap" in (root / "etc/hosts").read_text()


def test_splash_shows_the_logo_and_what_a_volatile_lab_is():
    text = volatile.splash()
    plain = text.replace("\033[1;34m", "").replace("\033[1;36m", "").replace("\033[1m", "").replace("\033[0m", "")
    assert logo.ART[5].strip() in plain
    assert "Volatile lab" in plain and "Nothing on this computer's disks is changed." in plain
    assert "\033[1;34m" in text  # the spark, in blue
    assert max(len(line) for line in plain.splitlines()) <= 80


def test_snap_pieces_go_into_the_system(tmp_path):
    root = tmp_path / "root"
    (root / "etc/systemd/system").mkdir(parents=True)
    (root / "etc/initramfs-tools").mkdir(parents=True)
    (root / "etc/initramfs-tools/modules").write_text("# modules\nloop\n")
    volatile.add_snap(root)
    assert "mountroot()" in (root / "etc/initramfs-tools/scripts/snaplive").read_text()
    modules = (root / "etc/initramfs-tools/modules").read_text().split()
    assert modules.count("loop") == 1 and "overlay" in modules and "squashfs" in modules
    assert os.access(root / "usr/local/sbin/snap-volatile", os.X_OK)
    assert (root / "usr/local/lib/snap/firstboot.sh").read_bytes() == volatile.firstboot()
    for unit, target in volatile.UNITS.items():
        assert (root / f"etc/systemd/system/{target}.wants/{unit}").is_symlink()
    assert "Before=pve-cluster.service" in (root / "etc/systemd/system/snap-volatile-hosts.service").read_text()
    assert (
        "After=snap-volatile-hosts.service" in (root / "etc/systemd/system/pve-cluster.service.d/snap.conf").read_text()
    )
    assert os.readlink(root / "etc/systemd/system/getty@tty1.service") == "/dev/null"


def test_identity_is_removed(tmp_path):
    root = tmp_path / "root"
    for f in (
        "etc/ssh/ssh_host_ed25519_key",
        "var/lib/pve-cluster/config.db",
        "etc/machine-id",
        "usr/sbin/policy-rc.d",
    ):
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text("x")
    volatile.strip_identity(root)
    assert not (root / "etc/ssh/ssh_host_ed25519_key").exists()
    assert not (root / "var/lib/pve-cluster/config.db").exists()
    assert (root / "etc/machine-id").read_text() == ""
    assert not (root / "usr/sbin/policy-rc.d").exists()


def test_building_the_system_needs_root(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(volatile.VolatileError, match="--privileged"):
        volatile.build_system(tmp_path / "pve.iso", "0" * 64, tmp_path)


def test_cache_key_follows_snap_pieces(monkeypatch):
    before = volatile.cache_key("a" * 64)
    assert volatile.cache_key("b" * 64) != before
    monkeypatch.setattr(volatile, "PRESEED", volatile.PRESEED + "x x x x\n")
    assert volatile.cache_key("a" * 64) != before


def test_volatile_menu_starts_the_system_from_the_stick():
    cfg = menu.render_volatile()
    assert "linux /boot/snap/vmlinuz boot=snaplive" in cfg and "initrd /boot/snap/initrd.img" in cfg
    assert "@KERNEL_EXTRA@" not in cfg and "console=ttyS0" not in cfg
    assert "console=ttyS0,115200" in menu.render_volatile(serial_console=True)
    files = menu.volatile_files()
    assert "Nothing on this computer is changed" in files["/boot/grub/snap/theme.txt"].decode()


@pytest.mark.skipif(shutil.which("grub-script-check") is None, reason="grub-script-check not installed")
def test_volatile_menu_is_valid_grub_script(tmp_path):
    cfg = tmp_path / "grub.cfg"
    cfg.write_text(menu.render_volatile())
    result = subprocess.run(["grub-script-check", str(cfg)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


def test_boot_scripts_are_valid_shell():
    for name in ("snaplive", "snap-splash"):
        result = subprocess.run(["sh", "-n", str(SCRIPT.parent / name)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
