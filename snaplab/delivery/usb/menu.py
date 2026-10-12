"""Render the SNAP boot menu (grub-snap.cfg) for a prepared Proxmox ISO.

The menu replaces Proxmox's own, but starts the installer exactly the way
Proxmox's "Install Proxmox VE (Automated)" entry does. Those lines are copied
from the ISO's menu at build time, so a Proxmox release that changes them is
picked up, and one that drops the entry stops the build instead of producing
a stick that cannot install.
"""

from __future__ import annotations

from importlib import resources

from snaplab.core import logo
from snaplab.delivery.usb import initrd

AUTO_ENTRY = "Install Proxmox VE (Automated)"
AUTO_FLAG = "proxmox-start-auto-installer"
# Kernel output on both the screen and the first serial port; the serial port
# becomes /dev/console, so the installer's messages appear there.
SERIAL_CONSOLE = " console=tty0 console=ttyS0,115200"
# The disk prompt's extra initrd, loaded after Proxmox's (initrd.py).
DISK_PROMPT = "/boot/snap/disk-prompt.img"


class MenuError(Exception):
    pass


def auto_install_entry(pve_cfg: str) -> tuple[str, str]:
    """Return the (linux, initrd) arguments of Proxmox's automated-install menu entry."""
    lines = pve_cfg.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("menuentry") and AUTO_ENTRY in line:
            linux = initrd = None
            for body in lines[i + 1 :]:
                words = body.split(None, 1)
                if not words:
                    continue
                if words[0] == "}":
                    break
                if words[0] == "linux" and len(words) == 2:
                    linux = " ".join(words[1].split())
                elif words[0] == "initrd" and len(words) == 2:
                    initrd = " ".join(words[1].split())
            if linux and initrd and AUTO_FLAG in linux.split():
                return linux, initrd
            break
    raise MenuError(
        f"the Proxmox ISO's boot menu has no usable '{AUTO_ENTRY}' entry; "
        "this Proxmox release is not supported by this builder"
    )


UNATTENDED = ("wizard", "text")


def render(pve_cfg: str, serial_console: bool = False, unattended: str | None = None) -> str:
    """The SNAP menu. For automated tests, `unattended` = "wizard" makes the guided setup
    run by itself with its defaults, "text" makes "no questions asked" the default."""
    linux, initrd = auto_install_entry(pve_cfg)
    # The guided setup starts Proxmox's graphical installer environment: the same
    # kernel line without the automatic-install flag.
    wizard_linux = " ".join(w for w in linux.split() if w != AUTO_FLAG)
    template = resources.files("snaplab.delivery.usb").joinpath("grub-snap.cfg").read_text()
    return (
        template.replace("@PVE_LINUX@", linux)
        .replace("@PVE_WIZARD_LINUX@", wizard_linux)
        .replace("@PVE_INITRD@", f"{initrd} {DISK_PROMPT}")
        .replace("@KERNEL_EXTRA@", SERIAL_CONSOLE if serial_console else "")
        .replace("@WIZARD_EXTRA@", "=auto" if unattended == "wizard" else "")
        .replace("@FRESH_DEFAULT@", "snap-auto" if unattended == "text" else "snap-wizard")
    )


LOGO_TOP = 24  # px; the theme puts the title and the menu below the logo
LOGO_LINE = 16  # px, the height of a line of the ISO's Unifont


def logo_labels() -> str:
    """Theme labels drawing the logo: each line in white, then the spark on top in blue."""
    white, spark = logo.white_and_spark()
    labels = []
    for layer, color in ((white, "#E6EDF3"), (spark, "#3B82F6")):
        for row, text in enumerate(layer):
            if not text.strip():
                continue
            labels.append(
                "+ label {\n"
                f"    left = 50%-{logo.WIDTH * 4}\n    top = {LOGO_TOP + row * LOGO_LINE}\n"
                f"    width = {logo.WIDTH * 8}\n    height = {LOGO_LINE}\n"
                f'    text = "{text}"\n    font = "Unifont Regular 16"\n    color = "{color}"\n}}'
            )
    return "\n".join(labels)


def files(
    pve_cfg: str,
    pve_initrd: bytes,
    serial_console: bool = False,
    unattended: str | None = None,
    wizard_files: dict[str, bytes] | None = None,
) -> dict[str, bytes]:
    """Everything the SNAP menu adds to the ISO, by path on the ISO."""
    out = {
        "/boot/grub/grub.cfg": render(pve_cfg, serial_console, unattended).encode(),
        DISK_PROMPT: initrd.overlay(pve_initrd, wizard_files or {}),
    }
    for item in resources.files("snaplab.delivery.usb").joinpath("theme").iterdir():
        data = item.read_bytes()
        if item.name == "theme.txt":
            data = data.decode().replace("@LOGO@", logo_labels()).encode()
        out[f"/boot/grub/snap/{item.name}"] = data
    return out
