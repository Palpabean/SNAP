"""Render the SNAP boot menu (grub-snap.cfg) for a prepared Proxmox ISO.

The menu replaces Proxmox's own, but starts the installer exactly the way
Proxmox's "Install Proxmox VE (Automated)" entry does. Those lines are copied
from the ISO's menu at build time, so a Proxmox release that changes them is
picked up, and one that drops the entry stops the build instead of producing
a stick that cannot install.
"""

from __future__ import annotations

from importlib import resources

AUTO_ENTRY = "Install Proxmox VE (Automated)"
AUTO_FLAG = "proxmox-start-auto-installer"
# Kernel output on both the screen and the first serial port; the serial port
# becomes /dev/console, so the installer's messages appear there.
SERIAL_CONSOLE = " console=tty0 console=ttyS0,115200"


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


def render(pve_cfg: str, serial_console: bool = False) -> str:
    linux, initrd = auto_install_entry(pve_cfg)
    template = resources.files("snaplab.delivery.usb").joinpath("grub-snap.cfg").read_text()
    return (
        template.replace("@PVE_LINUX@", linux)
        .replace("@PVE_INITRD@", initrd)
        .replace("@KERNEL_EXTRA@", SERIAL_CONSOLE if serial_console else "")
    )


def files(pve_cfg: str, serial_console: bool = False) -> dict[str, bytes]:
    """Everything the SNAP menu adds to the ISO, by path on the ISO."""
    out = {"/boot/grub/grub.cfg": render(pve_cfg, serial_console).encode()}
    for item in resources.files("snaplab.delivery.usb").joinpath("theme").iterdir():
        out[f"/boot/grub/snap/{item.name}"] = item.read_bytes()
    return out
