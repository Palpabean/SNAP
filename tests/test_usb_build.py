"""USB builder tests.

The payload tests run anywhere. The image tests need the disk-image tools
(xorriso, grub-mkrescue, mtools, dosfstools, e2fsprogs, sfdisk) and use a
stand-in ISO from grub-mkrescue, which has the same hybrid BIOS/UEFI layout as
the Proxmox ISO, plus a stub proxmox-auto-install-assistant. The boot tests
also need QEMU and OVMF. CI's usb-image job installs all of them.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from snaplab.delivery.usb import build, fetch, image

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "snap.example.yaml"
FIRSTBOOT = ROOT / "snaplab" / "stages" / "snap" / "firstboot.sh"
OVMF_CODE = Path("/usr/share/OVMF/OVMF_CODE_4M.fd")
OVMF_VARS = Path("/usr/share/OVMF/OVMF_VARS_4M.fd")

IMAGE_TOOLS = ["xorriso", "grub-mkrescue", "mkfs.vfat", "mcopy", "mtype", "mkfs.ext4", "debugfs", "sfdisk"]
needs_image_tools = pytest.mark.skipif(
    any(shutil.which(t) is None for t in IMAGE_TOOLS), reason="disk-image tools not installed"
)
needs_qemu = pytest.mark.skipif(
    shutil.which("qemu-system-x86_64") is None or not OVMF_CODE.exists(), reason="QEMU/OVMF not installed"
)

STUB_ASSISTANT = """#!/bin/sh
# Stand-in for proxmox-auto-install-assistant: prepare-iso copies the ISO unchanged.
case "$1" in
    --version) echo "proxmox-auto-install-assistant 9.2.8" ;;
    validate-answer) test -s "$2" ;;
    prepare-iso)
        iso="$2"; shift 2
        while [ $# -gt 0 ]; do
            case "$1" in --output) out="$2"; shift ;; esac
            shift
        done
        cp "$iso" "$out" ;;
    *) exit 2 ;;
esac
"""

STANDIN_GRUB_CFG = """serial --unit=0 --speed=115200
terminal_input --append serial
terminal_output --append serial
set timeout=1
menuentry 'stand-in installer' { echo STANDIN-INSTALLER-STARTED; halt }
echo "STANDIN-MENU platform=$grub_platform"
"""


# --- payload ---------------------------------------------------------------


def test_payload_manifest_satisfies_firstboot(tmp_path):
    payload = tmp_path / "payload"
    build.make_payload(EXAMPLE, payload)
    assert (payload / "snap.yaml").read_bytes() == EXAMPLE.read_bytes()
    assert os.access(payload / "bin" / "snap", os.X_OK)
    listed = [line.split("  ", 1)[1] for line in (payload / "payload.sha256").read_text().splitlines()]
    assert listed == ["bin/snap", "snap.yaml"]

    env = {**os.environ, "SNAP_ROOT": str(tmp_path / "root"), "SNAP_SOURCE": str(payload)}
    result = subprocess.run(["bash", str(FIRSTBOOT)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout
    assert (tmp_path / "root/var/lib/snap/snap.yaml").read_bytes() == EXAMPLE.read_bytes()


def test_layout_is_aligned():
    layout = image.plan(iso_size=1_706_178_560, data_size=20 * image.MIB + 1)
    for value in (layout.ais_start, layout.data_start, layout.data_size):
        assert value % image.MIB == 0
    assert layout.ais_start >= 1_706_178_560
    assert layout.data_start == layout.ais_start + image.AIS_SIZE
    assert layout.data_size == 21 * image.MIB


def test_missing_tools_are_reported(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(build.BuildError, match="missing tools: proxmox-auto-install-assistant"):
        build.check_tools()


# --- image -----------------------------------------------------------------


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    if any(shutil.which(t) is None for t in IMAGE_TOOLS):
        pytest.skip("disk-image tools not installed")
    tmp = tmp_path_factory.mktemp("usb")
    root = tmp / "isoroot" / "boot" / "grub"
    root.mkdir(parents=True)
    (root / "grub.cfg").write_text(STANDIN_GRUB_CFG)
    iso = tmp / "mirror" / "proxmox-ve_9.2-1.iso"
    iso.parent.mkdir()
    subprocess.run(["grub-mkrescue", "-o", str(iso), str(tmp / "isoroot")], check=True, capture_output=True)

    bindir = tmp / "bin"
    bindir.mkdir()
    stub = bindir / build.ASSISTANT
    stub.write_text(STUB_ASSISTANT)
    stub.chmod(0o755)
    lock = tmp / "checksums.lock"
    lock.write_text(
        f'[proxmox-ve]\nversion = "9.2-1"\nurl = "{iso.as_uri()}"\nsha256 = "{fetch.sha256(iso)}"\n'
        '[proxmox-auto-install-assistant]\nversion = "9.2.8"\n'
    )

    mp = pytest.MonkeyPatch()
    mp.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    # Keep dpkg-query from answering for a package that is not installed here.
    mp.setattr(build, "assistant_version", lambda: "9.2.8")
    try:
        out = build.build(EXAMPLE, tmp / "snap.img", cache=tmp / "cache", lock_path=lock, log=lambda _: None)
    finally:
        mp.undo()
    return out, iso


def partitions(img: Path) -> dict[str, dict]:
    table = json.loads(subprocess.run(["sfdisk", "-J", str(img)], check=True, capture_output=True, text=True).stdout)
    return {p.get("name", ""): p for p in table["partitiontable"]["partitions"]}


@needs_image_tools
def test_image_layout(built):
    img, iso = built
    parts = partitions(img)
    assert {"EFI boot partition", "PROXMOX-AIS", "SNAPDATA"} <= parts.keys()
    ais, data = parts["PROXMOX-AIS"], parts["SNAPDATA"]
    assert ais["start"] * 512 >= iso.stat().st_size
    assert data["start"] == ais["start"] + ais["size"]
    assert image.iso_uuid(img) == image.iso_uuid(iso)
    assert img.with_name("snap.img.sha256").read_text().split()[0] == fetch.sha256(img)


@needs_image_tools
def test_answer_partition(built):
    img, _ = built
    ais = partitions(img)["PROXMOX-AIS"]
    target = f"{img}@@{ais['start'] * 512}"
    label = subprocess.run(["mlabel", "-s", "-i", target, "::"], capture_output=True, text=True).stdout
    assert "PROXMOX-AIS" in label
    toml = subprocess.run(["mtype", "-i", target, "::answer.toml"], check=True, capture_output=True, text=True).stdout
    assert 'filter.ID_SERIAL_SHORT = "S4EWNX0R123456"' in toml


@needs_image_tools
def test_data_partition(built, tmp_path):
    img, _ = built
    data = partitions(img)["SNAPDATA"]
    part = tmp_path / "data.img"
    with img.open("rb") as f, part.open("wb") as out:
        f.seek(data["start"] * 512)
        out.write(f.read(data["size"] * 512))

    def cat(path):
        return subprocess.run(["debugfs", "-R", f"cat {path}", str(part)], check=True, capture_output=True).stdout

    assert cat("/snap.yaml") == EXAMPLE.read_bytes()
    assert b"bin/snap" in cat("/payload.sha256")


@needs_image_tools
def test_wrapped_boot_menu(built, tmp_path):
    img, _ = built
    out = tmp_path / "cfg"
    out.mkdir()
    subprocess.run(
        ["xorriso", "-osirrox", "on", "-indev", str(img), "-extract", "/boot/grub", str(out / "grub")],
        check=True,
        capture_output=True,
    )
    assert "(lvm/pve-root)" in (out / "grub" / "grub.cfg").read_text()
    assert (out / "grub" / "pve.cfg").read_text() == STANDIN_GRUB_CFG


# --- boot ------------------------------------------------------------------


def boot(img: Path, tmp_path: Path, uefi: bool) -> str:
    disk = tmp_path / "stick.img"
    shutil.copyfile(img, disk)
    cmd = ["qemu-system-x86_64", "-m", "512", "-nographic", "-no-reboot", "-nodefaults", "-serial", "stdio"]
    if uefi:
        vars_ = tmp_path / "vars.fd"
        shutil.copyfile(OVMF_VARS, vars_)
        cmd += [
            "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
            "-drive", f"if=pflash,format=raw,file={vars_}",
        ]  # fmt: skip
    cmd += ["-drive", f"file={disk},format=raw,if=virtio"]
    result = subprocess.run(cmd, capture_output=True, timeout=600)
    return result.stdout.decode(errors="replace")


@needs_image_tools
@needs_qemu
@pytest.mark.parametrize("uefi", [False, True], ids=["bios", "uefi"])
def test_boots_to_installer(built, tmp_path, uefi):
    img, _ = built
    out = boot(img, tmp_path, uefi)
    assert f"STANDIN-MENU platform={'efi' if uefi else 'pc'}" in out
    assert "STANDIN-INSTALLER-STARTED" in out


needs_lvm = pytest.mark.skipif(
    os.environ.get("SNAPLAB_TEST_LVM") != "1",
    reason="set SNAPLAB_TEST_LVM=1 on a machine with sudo, loop devices and LVM (CI's usb-image job does)",
)


@pytest.fixture
def installed_disk(tmp_path):
    """A disk laid out like an ext4 Proxmox VE install: VG pve, LV root, /boot/pve/vmlinuz."""
    disk = tmp_path / "target.img"
    disk.write_bytes(b"")
    os.truncate(disk, 96 * image.MIB)
    sudo = ["sudo", "-n"]
    loop = subprocess.run(
        sudo + ["losetup", "-f", "--show", str(disk)], check=True, capture_output=True, text=True
    ).stdout.strip()
    mnt = tmp_path / "mnt"
    mnt.mkdir()
    try:
        for cmd in (
            ["pvcreate", "-q", loop],
            ["vgcreate", "-q", "pve", loop],
            ["lvcreate", "-q", "-y", "-n", "root", "-l", "100%FREE", "pve"],
            ["mkfs.ext4", "-q", "/dev/pve/root"],
            ["mount", "/dev/pve/root", str(mnt)],
            ["mkdir", "-p", f"{mnt}/boot/pve"],
            ["sh", "-c", f"echo not-a-kernel > {mnt}/boot/pve/vmlinuz"],
            ["umount", str(mnt)],
            ["vgchange", "-q", "-an", "pve"],
        ):
            subprocess.run(sudo + cmd, check=True, capture_output=True)
    finally:
        subprocess.run(sudo + ["umount", str(mnt)], capture_output=True)
        subprocess.run(sudo + ["vgchange", "-q", "-an", "pve"], capture_output=True)
        subprocess.run(sudo + ["losetup", "-d", loop], capture_output=True)
    subprocess.run(sudo + ["chown", str(os.getuid()), str(disk)], check=True)
    return disk


@needs_image_tools
@needs_qemu
@needs_lvm
@pytest.mark.parametrize("uefi", [False, True], ids=["bios", "uefi"])
def test_existing_install_is_booted_not_reinstalled(built, installed_disk, tmp_path, uefi):
    img, _ = built
    stick = tmp_path / "stick.img"
    shutil.copyfile(img, stick)
    cmd = ["qemu-system-x86_64", "-m", "512", "-nographic", "-no-reboot", "-nodefaults", "-serial", "stdio"]
    if uefi:
        vars_ = tmp_path / "vars.fd"
        shutil.copyfile(OVMF_VARS, vars_)
        cmd += [
            "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
            "-drive", f"if=pflash,format=raw,file={vars_}",
        ]  # fmt: skip
    cmd += [
        "-drive", f"file={stick},format=raw,if=none,id=stick",
        "-device", "virtio-blk-pci,drive=stick,bootindex=0",
        "-drive", f"file={installed_disk},format=raw,if=none,id=target",
        "-device", "virtio-blk-pci,drive=target,bootindex=1",
    ]  # fmt: skip
    # The fake kernel cannot boot, so GRUB stops at an error; collect what it printed.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = proc.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    text = out.decode(errors="replace")
    assert "SNAP: Proxmox VE is already installed" in text
    assert "Starting the installed Proxmox VE" in text
    assert "STANDIN-INSTALLER-STARTED" not in text
