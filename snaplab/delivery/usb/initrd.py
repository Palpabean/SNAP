"""The disk prompt's small extra initrd (Design 0001, section 5.2).

GRUB loads it after Proxmox's own initrd; Linux unpacks both, and files in the
second replace those in the first. It holds Proxmox's start-up script (/init),
taken from the ISO at build time with one line added, and choose-disk.sh, which
that line runs just before the installer starts.
"""

from __future__ import annotations

import gzip
import lzma
import shutil
import subprocess
from importlib import resources

HOOK = "    . /snap/choose-disk.sh  # SNAP: choose the install disk (snaplab/delivery/usb/initrd.py)\n"
# The hook goes just before the installer is started, after the stick is
# mounted, every storage driver is loaded and the installer's files are in place.
ANCHOR = '    if [ -x "/mnt/.installer-mp/sbin/unconfigured.sh" ]; then\n'


class InitrdError(Exception):
    pass


def _decompress(data: bytes) -> bytes:
    if data[:4] == b"\x28\xb5\x2f\xfd":  # zstd
        if shutil.which("zstd") is None:
            raise InitrdError("the installer's initrd is zstd-compressed; install zstd")
        return subprocess.run(["zstd", "-dc"], input=data, capture_output=True, check=True).stdout
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    if data[:6] == b"\xfd7zXZ\x00":
        return lzma.decompress(data)
    raise InitrdError("unknown compression in the installer's initrd")


def _entries(archive: bytes):
    """(name, mode, data, end offset) for each member of a newc cpio archive, up to its trailer."""
    pos = 0
    while True:
        if archive[pos : pos + 6] not in (b"070701", b"070702"):
            raise InitrdError(f"not a cpio archive at offset {pos}")
        fields = [int(archive[pos + 6 + 8 * i : pos + 14 + 8 * i], 16) for i in range(13)]
        mode, filesize, namesize = fields[1], fields[6], fields[11]
        name_start = pos + 110
        name = archive[name_start : name_start + namesize - 1].decode()
        data_start = (name_start + namesize + 3) & ~3
        end = (data_start + filesize + 3) & ~3
        yield name, mode, archive[data_start : data_start + filesize], end
        if name == "TRAILER!!!":
            return
        pos = end


def read_init(initrd: bytes) -> str:
    """The /init script from an initrd (skipping an uncompressed early-microcode archive, if any)."""
    data = initrd
    while data[:6] in (b"070701", b"070702"):  # uncompressed archive(s) in front
        members = list(_entries(data))
        for name, _mode, content, _end in members:
            if name in ("init", "./init"):
                return content.decode()
        data = data[members[-1][3] :].lstrip(b"\0")
    for name, _mode, content, _end in _entries(_decompress(data)):
        if name in ("init", "./init"):
            return content.decode()
    raise InitrdError("the installer's initrd has no /init")


def patch_init(init: str) -> str:
    if init.count(ANCHOR) != 1:
        raise InitrdError(
            "the Proxmox installer's start-up script changed (no single place to add the disk prompt); "
            "this Proxmox release is not supported by this builder"
        )
    return init.replace(ANCHOR, HOOK + ANCHOR)


def _member(name: str, mode: int, data: bytes, ino: int) -> bytes:
    raw = name.encode() + b"\0"
    header = "070701" + "".join(
        f"{v:08x}" for v in (ino, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(raw), 0)
    )  # fmt: skip
    out = header.encode() + raw
    out += b"\0" * (-len(out) % 4) + data
    return out + b"\0" * (-len(out) % 4)


def archive(files: dict[str, tuple[int, bytes]]) -> bytes:
    """A newc cpio archive of {path: (mode, content)}; directories get mode 0o040755."""
    out = b""
    for ino, (name, (mode, data)) in enumerate(files.items(), 1):
        out += _member(name, mode, data, ino)
    out += _member("TRAILER!!!", 0, b"", 0)
    return out + b"\0" * (-len(out) % 512)


def overlay(pve_initrd: bytes) -> bytes:
    """The extra initrd: Proxmox's /init with the hook, and the disk prompt."""
    init = patch_init(read_init(pve_initrd))
    prompt = resources.files("snaplab.delivery.usb").joinpath("choose-disk.sh").read_bytes()
    return archive(
        {
            "init": (0o100755, init.encode()),
            "snap": (0o040755, b""),
            "snap/choose-disk.sh": (0o100644, prompt),
        }
    )
