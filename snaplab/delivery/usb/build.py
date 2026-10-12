"""SNAP Builder: turn a snap.yaml into a bootable USB image (Design 0001, section 5.1).

    snaplab build snap.yaml -o snap.img

Steps: validate the config, fetch and verify the pinned Proxmox ISO, render and
check the answer file, put it inside the ISO with Proxmox's own assistant,
then assemble the stick (see image.py). Downloads and prepared ISOs are cached.

Runs on Debian with proxmox-auto-install-assistant installed, most simply in
the container from snaplab/delivery/usb/Containerfile.
"""

from __future__ import annotations

import bz2
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from importlib import resources
from pathlib import Path

import yaml

import snaplab
from snaplab.core import config, defaults
from snaplab.delivery.usb import debs, fetch, image, initrd, menu, wizard_files
from snaplab.stages.snap import answer

ASSISTANT = "proxmox-auto-install-assistant"
TOOLS = [
    ASSISTANT,
    "xorriso",
    "mkfs.ext4",
    "sfdisk",
    "apt-get",
    "dpkg-deb",
    "openssl",
    "xz",
    "qemu-img",
    "sqfs2tar",
    "tar2sqfs",
]
REPO_LOCK = Path(__file__).resolve().parents[3] / "checksums.lock"
# What of the package goes on the stick for the host: the engine and the files
# the stages hand to OpenTofu and Ansible.
ENGINE_FILES = ["*.py", "*.tf", "*.yml"]


class BuildError(Exception):
    pass


def default_cache() -> Path:
    if env := os.environ.get("SNAPLAB_CACHE"):
        return Path(env)
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "snaplab"


def _asset(package: str, name: str) -> bytes:
    return resources.files(package).joinpath(name).read_bytes()


def check_tools() -> None:
    missing = [t for t in TOOLS if shutil.which(t) is None]
    if missing:
        raise BuildError(
            "missing tools: " + ", ".join(missing) + ". Run the builder in the container from "
            "snaplab/delivery/usb/Containerfile, or install them on Debian."
        )


def assistant_version() -> str:
    for cmd in (["dpkg-query", "-W", "-f", "${Version}", ASSISTANT], [ASSISTANT, "--version"]):
        try:
            out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
        except (OSError, subprocess.CalledProcessError):
            continue
        for word in out.split():
            if word[:1].isdigit():
                return word
    raise BuildError(f"cannot tell which {ASSISTANT} version is installed")


def prepare(iso: Path, answer_toml: str, firstboot: bytes, assistant: str, cache: Path, log=print) -> Path:
    """Run prepare-iso once per (ISO, answer file, first-boot script, assistant) combination.

    The answer file goes inside the ISO (--fetch-from iso). A separate answer
    partition on the same stick cannot work: the installer mounts the whole
    stick as its ISO, and Linux then refuses to mount one of its partitions.
    """
    parts = (fetch.sha256(iso).encode(), answer_toml.encode(), firstboot, assistant.encode())
    key = hashlib.sha256(b"\0".join(parts)).hexdigest()[:16]
    prepared = cache / f"{iso.stem}-snap-{key}.iso"
    if prepared.exists():
        log(f"using cached prepared ISO {prepared.name}")
        return prepared
    log("preparing the Proxmox ISO for unattended install")
    with tempfile.TemporaryDirectory(dir=cache) as tmp:
        script = Path(tmp) / "snap-firstboot.sh"
        script.write_bytes(firstboot)
        answer_file = Path(tmp) / "answer.toml"
        answer_file.write_text(answer_toml)
        partial = Path(tmp) / "prepared.iso"
        image.run(
            [
                ASSISTANT,
                "prepare-iso",
                str(iso),
                "--fetch-from",
                "iso",
                "--answer-file",
                str(answer_file),
                "--on-first-boot",
                str(script),
                "--output",
                str(partial),
            ]  # fmt: skip
        )
        partial.rename(prepared)
    return prepared


def make_payload(
    config_path: Path | None, cfg: dict, files: dict[str, Path], dest: Path, tools: dict | None = None
) -> None:
    """The SNAPDATA contents, copied to /var/lib/snap on the host at first boot.

    snap.yaml (as written) and snap.json (validated, for the host, which has no
    YAML library), the engine (lib/snaplab with its OpenTofu and Ansible files,
    bin/snap), `files` (payload path to
    source: VM images, tool archives, Debian packages), tools.json describing
    the bundled tools, and a checksum manifest over everything.
    """
    (dest / "bin").mkdir(parents=True)
    if config_path is not None:
        shutil.copyfile(config_path, dest / "snap.yaml")
    else:
        (dest / "snap.yaml").write_text(
            "# No snap.yaml was given: SNAP's built-in lab.\n" + yaml.safe_dump(cfg, sort_keys=False)
        )
    (dest / "snap.json").write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    command = dest / "bin" / "snap"
    command.write_bytes(_asset("snaplab.delivery.usb", "snap-host.sh"))
    command.chmod(0o755)
    package = Path(snaplab.__file__).parent
    engine_files = [p for pattern in ENGINE_FILES for p in package.rglob(pattern)]
    for src in engine_files:
        target = dest / "lib" / "snaplab" / src.relative_to(package)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
    for name, src in files.items():
        (dest / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest / name)
    (dest / "tools.json").write_text(json.dumps(tools or {}, indent=2, sort_keys=True) + "\n")
    files = sorted(p for p in dest.rglob("*") if p.is_file())
    manifest = "".join(f"{fetch.sha256(p)}  {p.relative_to(dest).as_posix()}\n" for p in files)
    (dest / "payload.sha256").write_text(manifest)


# The VM disk images on the stick, as plain disk images compressed with xz: the
# published files are a qcow2 with light zlib compression inside (the lab machine image) and
# bzip2 (OPNsense), which take 172 and 514 MiB; repacked, 73 and 343 MiB.
# This is what lets the download image fit in one release file.
LAB_IMAGE = "images/alpine-3.22-cloudinit.raw.xz"  # = crackle.stage.LAB_IMAGE
OPNSENSE_IMAGE = "images/opnsense-nano-amd64.img.xz"  # = pop.stage.OPNSENSE_ARCHIVE
QCOW2_MAGIC = b"QFI\xfb"


def repack(source: Path, log=print) -> Path:
    """`source` (qcow2, .bz2 or raw) as a raw disk image compressed with xz -9, made once
    next to it in the download cache."""
    out = source.with_name(source.name + ".raw.xz")
    if out.exists():
        return out
    log(f"repacking {source.name} (a few minutes, once)")
    raw = source.with_name(source.name + ".raw")
    try:
        with source.open("rb") as f:
            magic = f.read(4)
        if magic == QCOW2_MAGIC:
            image.run(["qemu-img", "convert", "-O", "raw", str(source), str(raw)])
        elif source.suffix == ".bz2":
            with bz2.open(source) as src, raw.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=4 * 1024 * 1024)
        else:
            shutil.copyfile(source, raw)
        tmp = out.with_name(out.name + ".partial")
        with tmp.open("wb") as dst:
            done = subprocess.run(["xz", "-T0", "-9", "--stdout", str(raw)], stdout=dst, stderr=subprocess.PIPE)
        if done.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise BuildError(f"xz could not compress {source.name}: {done.stderr.decode(errors='replace').strip()}")
        tmp.replace(out)
    finally:
        raw.unlink(missing_ok=True)
    return out


def repack_zip(source: Path) -> Path:
    """The same zip with LZMA instead of deflate (about a third smaller for these programs);
    Python's zipfile, which CRACKLE unpacks with, reads it. Made once, next to the download."""
    out = source.with_name(source.stem + ".lzma.zip")
    if out.exists():
        return out
    partial = out.with_name(out.name + ".partial")
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(partial, "w") as dst:
        for info in src.infolist():
            content = src.read(info)
            info.compress_type = zipfile.ZIP_LZMA  # modes and times stay in `info`
            dst.writestr(info, content)
    partial.replace(out)
    return out


# The Proxmox installer's own filesystem images, compressed with zstd. Rebuilt
# with xz they are 614 -> 564 and 108 -> 98 MiB: with the VM images above, what
# lets the download image fit in one 2 GiB release file. squashfs-tools-ng
# converts through tar, which keeps owners, modes and extended attributes
# without root; the installer's kernel reads xz squashfs.
INSTALLER_SQUASHFS = ["/pve-installer.squashfs", "/pve-base.squashfs"]


def repack_installer(prepared_iso: Path, iso_sha: str, cache: Path, log=print) -> dict[str, Path]:
    """Each of the ISO's squashfs images rebuilt with xz, made once per ISO in the cache."""
    found = {}
    for iso_path in INSTALLER_SQUASHFS:
        try:
            image.extents(prepared_iso, iso_path)
        except image.ImageError:
            continue  # not in this ISO (the tests' stand-in has none)
        out = cache / f"{iso_sha[:16]}-{iso_path.strip('/')}.xz"
        if not out.exists():
            log(f"repacking the installer's {iso_path.strip('/')} with xz (a few minutes, once)")
            with tempfile.TemporaryDirectory(dir=cache) as tmp:
                original = Path(tmp) / "original.squashfs"
                image.run(
                    ["xorriso", "-osirrox", "on", "-indev", str(prepared_iso), "-extract", iso_path, str(original)]
                )
                partial = Path(tmp) / "repacked.squashfs"
                sqfs2tar = subprocess.Popen(["sqfs2tar", str(original)], stdout=subprocess.PIPE)
                done = subprocess.run(
                    ["tar2sqfs", "--quiet", "--compressor", "xz",
                     "--comp-extra", "level=9,extreme,x86", "--block-size", "1048576", str(partial)],
                    stdin=sqfs2tar.stdout, capture_output=True,
                )  # fmt: skip
                sqfs2tar.stdout.close()
                if sqfs2tar.wait() != 0 or done.returncode != 0:
                    raise BuildError(f"could not repack {iso_path}: {done.stderr.decode(errors='replace').strip()}")
                partial.replace(out)
        found[iso_path] = out
    return found


def bundle_lab_files(lock: dict, cache: Path, log=print) -> tuple[dict[str, Path], dict]:
    """Fetch the pinned VM image and tool archives; return (payload files, tools.json)."""
    files: dict[str, Path] = {}
    machine = lock["lab-machine-image"]
    files[LAB_IMAGE] = repack(fetch.fetch(machine["url"], machine["sha256"], cache, log=log), log=log)

    opn = lock["opnsense"]
    files[OPNSENSE_IMAGE] = repack(fetch.fetch(opn["url"], opn["sha256"], cache, log=log), log=log)

    tofu, provider = lock["opentofu"], lock["opentofu-provider-proxmox"]
    files["tools/opentofu.zip"] = repack_zip(fetch.fetch(tofu["url"], tofu["sha256"], cache, log=log))
    files["tools/provider-proxmox.zip"] = repack_zip(fetch.fetch(provider["url"], provider["sha256"], cache, log=log))
    collections = []
    for c in lock["ansible-collection"]:
        archive = fetch.fetch(c["url"], c["sha256"], cache, log=log)
        files[f"collections/{archive.name}"] = archive
        collections.append({"name": c["name"], "version": c["version"], "file": f"collections/{archive.name}"})
    tools = {
        "opentofu": {"version": tofu["version"], "file": "tools/opentofu.zip"},
        "provider_proxmox": {
            "source": provider["source"],
            "version": provider["version"],
            "file": "tools/provider-proxmox.zip",
        },
        "ansible_collections": collections,
    }
    return files, tools


def bundle_debs(spec: dict, repo: Path, log=print) -> dict:
    """Download Ansible's Debian packages into a local repository; return what to install."""
    log("bundling Ansible from Debian")
    debs.download(spec["debian_packages"], repo)
    versions = debs.write_index(repo)
    core = versions.get("ansible-core", "")
    if not core.startswith(spec["ansible_core"]):
        raise BuildError(
            f"Debian offers ansible-core {core or 'none'}, but checksums.lock expects {spec['ansible_core']}*"
        )
    return {"install": spec["debian_packages"], "versions": {p: versions[p] for p in spec["debian_packages"]}}


def build(
    config_path: Path | None,
    output: Path,
    cache: Path | None = None,
    lock_path: Path = REPO_LOCK,
    serial_console: bool = False,
    unattended: str | None = None,
    release: bool = False,
    log=print,
) -> Path:
    """Build the stick. `release`: an image to publish for download, the same file for everyone,
    so it holds no password; each lab computer picks its own at first start."""
    if release:
        cfg, password_at_install = defaults.lock_passwords(config.load(config_path))
        password = None
    else:
        cfg, password = defaults.fill_passwords(config.load(config_path))
        password_at_install = False
    lock = fetch.load_lock(lock_path)
    cache = cache or default_cache()
    cache.mkdir(parents=True, exist_ok=True)
    check_tools()

    pinned = lock["proxmox-auto-install-assistant"]["version"]
    installed = assistant_version()
    if installed != pinned:
        raise BuildError(f"{ASSISTANT} {installed} is installed, but checksums.lock pins {pinned}")

    pve = lock["proxmox-ve"]
    iso = fetch.fetch(pve["url"], pve["sha256"], cache, log=log)
    files, tools = bundle_lab_files(lock, cache, log)
    firstboot = _asset("snaplab.stages.snap", "firstboot.sh")

    output = output.resolve()
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".snaplab-") as tmp:
        work = Path(tmp)
        answer_toml = answer.render(cfg)
        (work / "answer.toml").write_text(answer_toml)
        image.run([ASSISTANT, "validate-answer", str(work / "answer.toml")])
        prepared = prepare(iso, answer_toml, firstboot, installed, cache, log=log)

        repo = work / "debs"
        tools["debian_packages"] = bundle_debs(lock["ansible"], repo, log)
        files.update({f"debs/{f.name}": f for f in repo.iterdir()})
        if password_at_install:  # read by firstboot.sh
            flag = work / PASSWORD_AT_INSTALL
            flag.write_text("This stick holds no password; the lab computer picks its own at first start.\n")
            files[PASSWORD_AT_INSTALL] = flag
        make_payload(config_path, cfg, files, work / "payload", tools)
        image.make_data(work / "payload", work / "data.img")

        log("assembling the USB image")
        stick = work / "snap.img"
        try:
            pve_cfg = image.read_file(prepared, "/boot/grub/grub.cfg")
            _, pve_initrd = menu.auto_install_entry(pve_cfg)
            menu_files = menu.files(
                pve_cfg,
                image.read_bytes(prepared, pve_initrd),
                serial_console=serial_console,
                unattended=unattended,
                wizard_files=wizard_files.files(cfg),
            )
        except (menu.MenuError, initrd.InitrdError) as e:
            raise BuildError(str(e)) from e
        repacked = repack_installer(prepared, iso_sha=pve["sha256"], cache=cache, log=log)
        image.replace_boot_menu(prepared, {**menu_files, **repacked}, stick)
        image.blank_replaced(prepared, stick, list(repacked))
        image.assemble(stick, work / "data.img", stick)
        stick.replace(output)

    digest = fetch.sha256(output)
    output.with_name(output.name + ".sha256").write_text(f"{digest}  {output.name}\n")
    log(f"wrote {output} ({output.stat().st_size // image.MIB} MiB, sha256 {digest})")
    if password:
        note = password_file(output)
        note.write_text(
            f"SNAP lab built as {output.name}\n\n"
            f"Password: {password}\n\n"
            "It logs you in as root on Proxmox (web page at https://<this computer>:8006) and on the\n"
            f"router, and as '{cfg['machines']['username']}' on the lab machines, wherever no other\n"
            "password was set. Keep this file private.\n"
        )
        note.chmod(0o600)
        log(f"password for the lab: {password}  (also saved in {note.name})")
    return output


PASSWORD_AT_INSTALL = "password-at-install"


def password_file(output: Path) -> Path:
    return output.with_name(output.name + ".password.txt")
