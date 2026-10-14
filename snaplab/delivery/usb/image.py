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


def read_bytes(iso: Path, path: str) -> bytes:
    """Read one file from an ISO."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "file"
        run(["xorriso", "-osirrox", "on", "-indev", str(iso), "-extract", path, str(out)])
        return out.read_bytes()


def read_file(iso: Path, path: str) -> str:
    return read_bytes(iso, path).decode()


def extents(iso: Path, path: str) -> list[tuple[int, int]]:
    """Where a file's data lies in the ISO, as (byte offset, length) pairs."""
    out = run(["xorriso", "-indev", str(iso), "-find", path, "-exec", "report_lba", "--"]).stdout
    found = []
    for line in out.splitlines():
        # File data lba:  0 ,  33468 ,  314439 ,  643975168 , '/pve-installer.squashfs'
        if line.startswith("File data lba:"):
            fields = [f.strip() for f in line.split(":", 1)[1].split(",")]
            found.append((int(fields[1]) * 2048, int(fields[3])))
    if not found:
        raise ImageError(f"{path} is not in {iso.name}")
    return found


def replace_boot_menu(
    prepared_iso: Path, files: dict[str, bytes | Path], out: Path, remove: list[str] | None = None
) -> None:
    """Copy the ISO and replace or add `files` (ISO path to content or a local file), leaving
    its boot setup untouched.

    The change is appended as a new ISO session instead of rebuilding the image,
    so the boot code, partition tables and El Torito records stay byte for byte
    as Proxmox made them. The volume UUID, which Proxmox's EFI GRUB searches
    for, is kept. `remove`: files or folders the new session leaves out.
    """
    uuid = iso_uuid(prepared_iso)
    shutil.copyfile(prepared_iso, out)
    with tempfile.TemporaryDirectory() as tmp:
        maps = []
        for i, (iso_path, content) in enumerate(files.items()):
            if isinstance(content, Path):
                local = content
            else:
                local = Path(tmp) / str(i)
                local.write_bytes(content)
            maps += ["-map", str(local), iso_path]
        removals = [arg for path in remove or [] for arg in ("-rm_r", path, "--")]
        run(
            ["xorriso", "-dev", str(out), "-boot_image", "any", "keep", "-volume_date", "uuid", uuid,
             *removals, *maps, "-commit"]
        )  # fmt: skip
    if iso_uuid(out) != uuid:
        raise ImageError("the ISO lost its volume UUID; it would not boot on UEFI")
    with prepared_iso.open("rb") as a, out.open("rb") as b:
        if a.read(SYSTEM_AREA) != b.read(SYSTEM_AREA):
            raise ImageError("the ISO's boot code or partition table changed; refusing it")


def blank_replaced(prepared_iso: Path, out: Path, paths: list[str]) -> None:
    """Zero the old data of files the appended session replaced. Nothing refers to it any more,
    but it is still in the image; zeros cost nothing once the image is compressed."""
    for path in paths:
        old = extents(prepared_iso, path)
        new = extents(out, path)
        for start, length in old:
            if any(start < n_start + n_len and n_start < start + length for n_start, n_len in new):
                raise ImageError(f"{path}: the new copy overlaps the old one; not blanking it")
        with out.open("r+b") as f:
            for start, length in old:
                f.seek(start)
                for done in range(0, length, 4 * MIB):
                    f.write(bytes(min(4 * MIB, length - done)))


def blank_removed(original_iso: Path, out: Path, paths: list[str]) -> None:
    """Zero the data of files and folders the appended session removed (replace_boot_menu's
    `remove`): nothing refers to it any more, and zeros cost nothing once compressed."""
    old = [extent for path in paths for extent in extents(original_iso, path)]
    for path in paths:
        try:
            extents(out, path)
        except ImageError:
            continue
        raise ImageError(f"{path} is still in the image; not blanking it")
    with out.open("r+b") as f:
        for start, length in old:
            f.seek(start)
            for done in range(0, length, 4 * MIB):
                f.write(bytes(min(4 * MIB, length - done)))


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
