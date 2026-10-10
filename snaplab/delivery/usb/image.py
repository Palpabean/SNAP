"""Assemble the SNAP USB image (Design 0001, sections 5.2 and D1).

The stick is the Proxmox installer's own hybrid ISO, written from the first
sector exactly as Proxmox intends, with two partitions appended after it:

    | Proxmox ISO (prepared, answer file inside, SNAP boot menu) | SNAPDATA (ext4) |

The answer file lives inside the ISO: the installer mounts the whole stick as
its ISO, and Linux then refuses to mount a partition of the same stick.

Everything happens in a regular file with xorriso, mkfs.ext4 and sfdisk, so no
loop devices or root privileges are needed.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

SECTOR = 512
MIB = 1 << 20
ALIGN = MIB
# Room after the last partition for the backup GPT.
TAIL = MIB

TYPE_LINUX_DATA = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"

DATA_LABEL = "SNAPDATA"
# The ISO's first 32 KiB: boot code, MBR and GPT.
SYSTEM_AREA = 32 * 1024


class ImageError(Exception):
    pass


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)
    except FileNotFoundError as e:
        raise ImageError(f"{cmd[0]} is not installed") from e
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip().splitlines()[-5:]
        raise ImageError(f"{' '.join(cmd[:2])} failed: " + " / ".join(detail)) from e


def align_up(n: int, to: int = ALIGN) -> int:
    return -(-n // to) * to


@dataclass(frozen=True)
class Layout:
    """Byte offset and size of the appended SNAPDATA partition."""

    data_start: int
    data_size: int

    @property
    def total(self) -> int:
        return self.data_start + self.data_size + TAIL


def plan(iso_size: int, data_size: int) -> Layout:
    return Layout(align_up(iso_size), align_up(data_size))


# --- ISO9660 volume UUID -------------------------------------------------


def iso_uuid(path: Path) -> str:
    """The UUID GRUB uses to find this ISO: the volume modification date, else the creation date.

    The EFI GRUB in Proxmox's ISO searches for this UUID, so a remastered ISO must keep it.
    """
    with path.open("rb") as f:
        f.seek(16 * 2048)
        pvd = f.read(2048)
    if pvd[1:6] != b"CD001":
        raise ImageError(f"{path} is not an ISO9660 image")
    created, modified = pvd[813:829], pvd[830:846]
    stamp = modified if modified.strip(b"0\x00 ") else created
    if not stamp.isdigit():
        raise ImageError(f"{path} has no usable volume date")
    return stamp.decode()


# --- steps -----------------------------------------------------------------


def read_file(iso: Path, path: str) -> str:
    """Read one file from an ISO."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "file"
        run(["xorriso", "-osirrox", "on", "-indev", str(iso), "-extract", path, str(out)])
        return out.read_text()


def replace_boot_menu(prepared_iso: Path, files: dict[str, bytes], out: Path) -> None:
    """Copy the ISO and replace or add `files` (ISO path to content), leaving its boot setup untouched.

    The change is appended as a new ISO session instead of rebuilding the image,
    so the boot code, partition tables and El Torito records stay byte for byte
    as Proxmox made them. The volume UUID, which Proxmox's EFI GRUB searches
    for, is kept.
    """
    uuid = iso_uuid(prepared_iso)
    shutil.copyfile(prepared_iso, out)
    with tempfile.TemporaryDirectory() as tmp:
        maps = []
        for i, (iso_path, content) in enumerate(files.items()):
            local = Path(tmp) / str(i)
            local.write_bytes(content)
            maps += ["-map", str(local), iso_path]
        run(["xorriso", "-dev", str(out), "-boot_image", "any", "keep", "-volume_date", "uuid", uuid, *maps, "-commit"])
    if iso_uuid(out) != uuid:
        raise ImageError("the ISO lost its volume UUID; it would not boot on UEFI")
    with prepared_iso.open("rb") as a, out.open("rb") as b:
        if a.read(SYSTEM_AREA) != b.read(SYSTEM_AREA):
            raise ImageError("the ISO's boot code or partition table changed; refusing it")


def make_data(payload: Path, out: Path) -> None:
    """The ext4 SNAPDATA partition, filled from the payload directory."""
    used = sum(f.stat().st_size for f in payload.rglob("*") if f.is_file())
    size = align_up(int(used * 1.2) + 16 * MIB)
    out.unlink(missing_ok=True)
    run(
        [
            "mkfs.ext4",
            "-q",
            "-F",
            "-L",
            DATA_LABEL,
            "-d",
            str(payload),
            "-E",
            "root_owner=0:0",
            "-O",
            "^has_journal",
            str(out),
            f"{size // 1024}k",
        ]  # fmt: skip
    )


def assemble(iso: Path, data: Path, out: Path) -> Layout:
    """Append the SNAPDATA partition to the ISO and record it in its GPT."""
    layout = plan(iso.stat().st_size, data.stat().st_size)
    if out != iso:
        raise ImageError("assemble works in place on the ISO")

    with out.open("r+b") as f:
        f.truncate(layout.total)
        f.seek(layout.data_start)
        with data.open("rb") as s:
            while chunk := s.read(MIB):
                f.write(chunk)

    script = (
        f"start={layout.data_start // SECTOR}, size={layout.data_size // SECTOR}, "
        f'type={TYPE_LINUX_DATA}, name="{DATA_LABEL}"\n'
    )
    run(["sfdisk", "--no-reread", "--no-tell-kernel", "--append", "--wipe", "never", str(out)], input=script)
    return layout
