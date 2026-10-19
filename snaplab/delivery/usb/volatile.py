"""The volatile lab's ready-made Proxmox system (snaplab build --volatile).

A volatile stick runs Proxmox straight from the USB stick and the computer's
memory, so it needs Proxmox already installed. The Proxmox ISO holds no such
system: its pve-base.squashfs is a small Debian, and the installer adds
Proxmox from the ISO's own packages (/proxmox/packages) on every install.
This does the same once, at build time, in a chroot:

  1. unpack pve-base.squashfs,
  2. install every package from the ISO, as Proxmox's installer does
     (unpack all, then configure; services are kept from starting),
  3. add SNAP's volatile pieces (the snaplive boot script, the setup units, the
     start-up screen, firstboot.sh) and make the kernel's initrd with them,
  4. remove what makes a system one particular machine (machine ID, SSH and
     Proxmox keys), which each start makes afresh,
  5. pack it as pve-root.squashfs (xz), with the kernel and initrd beside it.

The result is cached per ISO and per version of SNAP's pieces. Needs root
(chroot, mounting /proc): run the builder container with --privileged.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from snaplab.core import logo

ASSETS = "live"  # snaplab/delivery/usb/live/
# Packages whose setup installs a boot loader on a disk, which fails in a folder and is not
# wanted: the volatile lab boots from the stick's own GRUB. Their setup scripts are skipped
# when they fail, so that dpkg records them as installed; otherwise apt sees a broken system
# and refuses to install anything later (CRACKLE installs Ansible).
BOOT_LOADER_PREFIXES = ("grub", "shim", "proxmox-grub", "proxmox-secure-boot")


def boot_loader(package: str) -> bool:
    return package.startswith(BOOT_LOADER_PREFIXES)


# Answers for the packages that ask questions while being configured.
PRESEED = """\
postfix postfix/main_mailer_type select Local only
postfix postfix/mailname string snap.lab.internal
grub-pc grub-pc/install_devices multiselect
grub-pc grub-pc/install_devices_empty boolean true
"""
UNITS = {
    "snap-volatile-early.service": "sysinit.target",
    "snap-volatile-hosts.service": "multi-user.target",
    "snap-volatile-handoff.service": "multi-user.target",
    "snap-splash.service": "sysinit.target",
}
INITRD_MODULES = ["squashfs", "overlay", "loop", "ext4", "usb_storage", "uas", "sd_mod", "virtio_blk", "virtio_pci"]


class VolatileError(Exception):
    pass


@dataclass(frozen=True)
class System:
    """The ready-made Proxmox system and how to start it."""

    squashfs: Path
    kernel: Path
    initrd: Path


def asset(name: str) -> bytes:
    return resources.files("snaplab.delivery.usb").joinpath(ASSETS, name).read_bytes()


def asset_names() -> list[str]:
    return sorted(p.name for p in resources.files("snaplab.delivery.usb").joinpath(ASSETS).iterdir() if p.is_file())


def firstboot() -> bytes:
    return resources.files("snaplab.stages.snap").joinpath("firstboot.sh").read_bytes()


def splash(width: int = 80) -> str:
    """The start-up screen's top half: the SNAP logo (spark in blue), the title, and what a
    volatile lab is, centred for an 80-column console."""
    pad = " " * max(0, (width - logo.WIDTH) // 2)
    rows = []
    for row in range(len(logo.ART)):
        line = "".join(f"\033[1;34m{text}\033[0m" if blue else text for text, blue in logo.runs(row))
        rows.append((pad + line).rstrip())

    def centre(text: str, style: str = "") -> str:
        line = " " * max(0, (width - len(text)) // 2) + text
        return f"{style}{line}\033[0m" if style else line

    return "\n".join(
        ["", *rows, "", centre("S N A P   -   Swift Network Automation Program", "\033[1;36m"),
         centre("Volatile lab", "\033[1m"), "",
         centre("Runs from this USB stick and the computer's memory."),
         centre("Nothing on this computer's disks is changed."),
         centre("Everything is gone when you turn it off or remove the stick."), ""]
    )  # fmt: skip


def cache_key(iso_sha: str) -> str:
    parts = [iso_sha.encode(), firstboot(), splash().encode(), PRESEED.encode()]
    parts += [name.encode() + b"\0" + asset(name) for name in asset_names()]
    return hashlib.sha256(b"\0".join(parts)).hexdigest()[:16]


def _run(cmd: list[str], log_file: Path | None = None, **kwargs) -> subprocess.CompletedProcess:
    try:
        if log_file is not None:
            with log_file.open("a") as out:
                return subprocess.run(cmd, check=True, stdout=out, stderr=subprocess.STDOUT, **kwargs)
        return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)
    except FileNotFoundError as e:
        raise VolatileError(f"{cmd[0]} is not installed") from e
    except subprocess.CalledProcessError as e:
        tail = ""
        if log_file is not None and log_file.exists():
            tail = " / ".join(log_file.read_text(errors="replace").strip().splitlines()[-8:])
        else:
            tail = " / ".join((e.stderr or e.stdout or "").strip().splitlines()[-8:])
        raise VolatileError(f"{' '.join(cmd[:3])} failed: {tail}") from e


CHROOT_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C"}


def mentions(log_file: Path, packages: list[str], limit: int = 12) -> str:
    """dpkg's last lines about `packages`, to say why they could not be set up."""
    if not log_file.exists():
        return "no log"
    lines = [
        line.strip()
        for line in log_file.read_text(errors="replace").splitlines()
        if any(p in line for p in packages) and ("depend" in line or "error" in line or "not" in line)
    ]
    return " / ".join(lines[-limit:]) or "no details in the log"


class Chroot:
    """/proc, /sys and /dev mounted in `root` while in use, for package scripts."""

    MOUNTS = [("proc", ["-t", "proc", "proc"]), ("sys", ["-t", "sysfs", "sys"]),
              ("dev", ["--bind", "/dev"]), ("dev/pts", ["--bind", "/dev/pts"])]  # fmt: skip

    def __init__(self, root: Path):
        self.root = root
        self.mounted: list[Path] = []

    def __enter__(self) -> Chroot:
        for name, args in self.MOUNTS:
            target = self.root / name
            target.mkdir(parents=True, exist_ok=True)
            _run(["mount", *args, str(target)])
            self.mounted.append(target)
        return self

    def __exit__(self, *exc) -> None:
        for target in reversed(self.mounted):
            subprocess.run(["umount", "-l", str(target)], capture_output=True)
        self.mounted.clear()

    def run(self, *cmd: str, log_file: Path | None = None) -> subprocess.CompletedProcess:
        return _run(["chroot", str(self.root), *cmd], log_file=log_file, env=CHROOT_ENV)


UNFINISHED = {"unpacked", "half-configured", "half-installed", "triggers-awaited", "triggers-pending"}


def unconfigured(chroot: Chroot) -> set[str]:
    """Packages dpkg left installed but not set up."""
    out = chroot.run("dpkg-query", "-W", "-f", "${Package} ${Status}\\n").stdout
    return {words[0] for words in map(str.split, out.splitlines()) if words and words[-1] in UNFINISHED}


def install_packages(root: Path, packages: list[Path], work: Path, log=print) -> None:
    """Every Proxmox package from the ISO into `root`, the way Proxmox's installer does it."""
    debs = root / "tmp/snap-debs"
    debs.mkdir(parents=True)
    for deb in packages:
        shutil.copyfile(deb, debs / deb.name)
    (root / "tmp/snap-preseed").write_text(PRESEED)
    # Services must not start inside the chroot.
    policy = root / "usr/sbin/policy-rc.d"
    policy.write_text("#!/bin/sh\nexit 101\n")
    policy.chmod(0o755)
    (root / "etc/hostname").write_text("snap\n")
    (root / "etc/hosts").write_text("127.0.0.1 localhost\n127.0.1.1 snap.lab.internal snap\n")
    log_file = work / "packages.log"
    try:
        with Chroot(root) as chroot:
            chroot.run("debconf-set-selections", "/tmp/snap-preseed")
            log(f"installing {len(packages)} Proxmox packages into the volatile system (several minutes)")
            chroot.run(
                "sh", "-c",
                "dpkg --force-depends --no-triggers --force-unsafe-io --force-confold --unpack /tmp/snap-debs/*.deb",
                log_file=log_file,
            )  # fmt: skip
            stubbed: set[str] = set()
            for _ in range(4):
                try:
                    chroot.run("dpkg", "--force-confold", "--configure", "-a", log_file=log_file)
                    break
                except VolatileError:
                    pass  # judged below, package by package
                stuck = {p for p in unconfigured(chroot) if boot_loader(p)} - stubbed
                if not stuck:
                    break
                for package in stuck:
                    script = root / f"var/lib/dpkg/info/{package}.postinst"
                    if script.exists():
                        script.write_text("#!/bin/sh\n# SNAP volatile lab: no boot loader to install here.\nexit 0\n")
                stubbed |= stuck
                log(f"skipping the boot-loader setup of {', '.join(sorted(stuck))}")
            # Boot-loader packages that still cannot be set up (their dependencies are about
            # installing a boot loader, not running Proxmox) are removed.
            leftover = sorted(p for p in unconfigured(chroot) if boot_loader(p))
            if leftover:
                why = mentions(log_file, leftover)
                log(f"leaving out {', '.join(leftover)}: {why}")
                purge = ["dpkg", "--purge", "--force-depends", "--force-remove-reinstreq", *leftover]
                chroot.run(*purge, log_file=log_file)
            left = unconfigured(chroot)
            if left:
                raise VolatileError(
                    "these Proxmox packages could not be set up: " + ", ".join(sorted(left)) + ": "
                    + mentions(log_file, sorted(left))
                )  # fmt: skip
            # What CRACKLE's apt-get will find: a consistent system, every dependency met.
            check = subprocess.run(
                ["chroot", str(root), "apt-get", "check"], capture_output=True, text=True, env=CHROOT_ENV
            )
            if check.returncode != 0:
                detail = " / ".join((check.stdout + check.stderr).strip().splitlines()[-15:])
                raise VolatileError(f"the volatile system's packages are inconsistent: {detail}")
    finally:
        shutil.rmtree(debs, ignore_errors=True)
        (root / "tmp/snap-preseed").unlink(missing_ok=True)


def add_snap(root: Path) -> None:
    """SNAP's volatile pieces: boot script, setup units, start-up screen, firstboot.sh."""
    scripts = root / "etc/initramfs-tools/scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "snaplive").write_bytes(asset("snaplive"))
    modules = root / "etc/initramfs-tools/modules"
    existing = modules.read_text() if modules.exists() else ""
    modules.write_text(existing + "".join(f"{m}\n" for m in INITRD_MODULES if m not in existing.split()))

    sbin = root / "usr/local/sbin"
    sbin.mkdir(parents=True, exist_ok=True)
    for name in ("snap-volatile", "snap-splash"):
        (sbin / name).write_bytes(asset(name))
        (sbin / name).chmod(0o755)
    lib = root / "usr/local/lib/snap"
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "firstboot.sh").write_bytes(firstboot())
    (lib / "firstboot.sh").chmod(0o755)
    (lib / "splash.txt").write_text(splash() + "\n")

    systemd = root / "etc/systemd/system"
    for unit, target in UNITS.items():
        (systemd / unit).write_bytes(asset(unit))
        wants = systemd / f"{target}.wants"
        wants.mkdir(parents=True, exist_ok=True)
        link = wants / unit
        link.unlink(missing_ok=True)
        link.symlink_to(f"/etc/systemd/system/{unit}")
    dropin = systemd / "pve-cluster.service.d"
    dropin.mkdir(parents=True, exist_ok=True)
    (dropin / "snap.conf").write_bytes(asset("pve-cluster-snap.conf"))
    # The start-up screen keeps the first console until SNAP's own screen takes it;
    # a login prompt stays on the others (Alt+F2).
    getty = systemd / "getty@tty1.service"
    getty.unlink(missing_ok=True)
    getty.symlink_to("/dev/null")
    (root / "etc/fstab").write_text("# SNAP volatile lab: / is the stick's system with a RAM layer (snaplive).\n")


def make_initrd(root: Path, kernel_version: str, work: Path) -> None:
    with Chroot(root) as chroot:
        chroot.run("update-initramfs", "-c", "-k", kernel_version, log_file=work / "initramfs.log")


def strip_identity(root: Path) -> None:
    """What makes this one machine; each start of the volatile lab makes it afresh."""
    for pattern in ("etc/ssh/ssh_host_*", "var/lib/pve-cluster/config.db*", "var/lib/dbus/machine-id",
                    "var/lib/systemd/random-seed", "root/.ssh/*", "etc/pve/*"):  # fmt: skip
        for p in root.glob(pattern):
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink()
    (root / "etc/machine-id").write_text("")  # empty: systemd makes one at each start
    (root / "usr/sbin/policy-rc.d").unlink(missing_ok=True)
    shutil.rmtree(root / "var/cache/apt/archives", ignore_errors=True)
    (root / "var/cache/apt/archives/partial").mkdir(parents=True)


def kernel_version(root: Path) -> str:
    modules = (root / "lib/modules").iterdir()
    versions = sorted(p.name for p in modules if (root / "boot" / f"vmlinuz-{p.name}").exists())
    if not versions:
        raise VolatileError("the Proxmox packages did not install a kernel")
    return versions[-1]


def check_root() -> None:
    if os.geteuid() != 0:
        raise VolatileError(
            "building a volatile stick installs Proxmox into a folder, which needs root: "
            "run the builder container with --privileged (docker run --privileged ...)"
        )


def extract(iso: Path, dest: Path) -> None:
    """The base system and the packages from the Proxmox ISO."""
    for path in ("/pve-base.squashfs", "/proxmox/packages"):
        _run(["xorriso", "-osirrox", "on", "-indev", str(iso), "-extract", path, str(dest / path.lstrip("/"))])


def build_system(iso: Path, iso_sha: str, cache: Path, log=print) -> System:
    """The ready-made Proxmox system from the Proxmox ISO, made once and cached."""
    out = cache / f"volatile-{iso_sha[:16]}-{cache_key(iso_sha)}"
    system = System(out / "pve-root.squashfs", out / "vmlinuz", out / "initrd.img")
    if all(p.exists() for p in (system.squashfs, system.kernel, system.initrd)):
        log(f"using cached volatile system {out.name}")
        return system
    check_root()
    with tempfile.TemporaryDirectory(dir=cache, prefix=".volatile-") as tmp:
        work = Path(tmp)
        iso_dir = work / "iso"
        log("reading Proxmox's base system and packages from the ISO")
        extract(iso, iso_dir)
        packages = sorted((iso_dir / "proxmox/packages").glob("*.deb"))
        if not packages:
            raise VolatileError("the Proxmox ISO has no packages in /proxmox/packages")
        root = work / "root"
        log("unpacking Proxmox's base system")
        _run(["unsquashfs", "-q", "-d", str(root), str(iso_dir / "pve-base.squashfs")])
        install_packages(root, packages, work, log=log)
        shutil.rmtree(iso_dir)
        add_snap(root)
        version = kernel_version(root)
        make_initrd(root, version, work)
        partial = out.with_name(out.name + ".partial")
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir(parents=True)
        shutil.copyfile(root / "boot" / f"vmlinuz-{version}", partial / "vmlinuz")
        shutil.copyfile(root / "boot" / f"initrd.img-{version}", partial / "initrd.img")
        strip_identity(root)
        log("packing the volatile system (several minutes)")
        _run(["mksquashfs", str(root), str(partial / "pve-root.squashfs"), "-comp", "xz", "-Xbcj", "x86",
              "-b", "1M", "-noappend", "-quiet", "-e", "boot"])  # fmt: skip
        shutil.rmtree(out, ignore_errors=True)
        partial.rename(out)
    log(f"volatile system: {system.squashfs.stat().st_size // (1 << 20)} MiB")
    return system
