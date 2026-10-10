"""SNAP Builder: turn a snap.yaml into a bootable USB image (Design 0001, section 5.1).

    snaplab build snap.yaml -o snap.img

Steps: validate the config, fetch and verify the pinned Proxmox ISO, render and
check the answer file, put it inside the ISO with Proxmox's own assistant,
then assemble the stick (see image.py). Downloads and prepared ISOs are cached.

Runs on Debian with proxmox-auto-install-assistant installed, most simply in
the container from snaplab/delivery/usb/Containerfile.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from importlib import resources
from pathlib import Path

from snaplab.core import config
from snaplab.delivery.usb import fetch, image, menu
from snaplab.stages.snap import answer

ASSISTANT = "proxmox-auto-install-assistant"
TOOLS = [ASSISTANT, "xorriso", "mkfs.ext4", "sfdisk"]
REPO_LOCK = Path(__file__).resolve().parents[3] / "checksums.lock"


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


def make_payload(config_path: Path, dest: Path) -> None:
    """The SNAPDATA contents: snap.yaml, the engine, and a checksum manifest over both."""
    (dest / "bin").mkdir(parents=True)
    shutil.copyfile(config_path, dest / "snap.yaml")
    engine = dest / "bin" / "snap"
    engine.write_bytes(_asset("snaplab.delivery.usb", "engine-placeholder.sh"))
    engine.chmod(0o755)
    files = sorted(p for p in dest.rglob("*") if p.is_file())
    manifest = "".join(f"{fetch.sha256(p)}  {p.relative_to(dest).as_posix()}\n" for p in files)
    (dest / "payload.sha256").write_text(manifest)


def build(
    config_path: Path,
    output: Path,
    cache: Path | None = None,
    lock_path: Path = REPO_LOCK,
    serial_console: bool = False,
    log=print,
) -> Path:
    cfg = config.load(config_path)
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
    firstboot = _asset("snaplab.stages.snap", "firstboot.sh")

    output = output.resolve()
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".snaplab-") as tmp:
        work = Path(tmp)
        answer_toml = answer.render(cfg)
        (work / "answer.toml").write_text(answer_toml)
        image.run([ASSISTANT, "validate-answer", str(work / "answer.toml")])
        prepared = prepare(iso, answer_toml, firstboot, installed, cache, log=log)

        make_payload(config_path, work / "payload")
        image.make_data(work / "payload", work / "data.img")

        log("assembling the USB image")
        stick = work / "snap.img"
        try:
            menu_files = menu.files(image.read_file(prepared, "/boot/grub/grub.cfg"), serial_console)
        except menu.MenuError as e:
            raise BuildError(str(e)) from e
        image.replace_boot_menu(prepared, menu_files, stick)
        image.assemble(stick, work / "data.img", stick)
        stick.replace(output)

    digest = fetch.sha256(output)
    output.with_name(output.name + ".sha256").write_text(f"{digest}  {output.name}\n")
    log(f"wrote {output} ({output.stat().st_size // image.MIB} MiB, sha256 {digest})")
    return output
