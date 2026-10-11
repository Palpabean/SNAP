"""The host's side of the admin network (D8).

The Proxmox host gets an address on the admin VLAN of the lab bridge, with
routes to the lab networks through the router, so Ansible can reach the lab
machines and OpenTofu never needs to. The file lives in interfaces.d, which
Proxmox's own network configuration includes and its web interface leaves alone.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from snaplab.core.engine import Context
from snaplab.stages.pop.opnsense import ADMIN_CIDR, ADMIN_HOST, ADMIN_ROUTER, ADMIN_VLAN

LAB_BRIDGE = "vmbr1"
IFACE = f"{LAB_BRIDGE}.{ADMIN_VLAN}"
CONFIG = "/etc/network/interfaces.d/snap-admin"
MAIN_CONFIG = "/etc/network/interfaces"


def render(cfg: dict[str, Any]) -> str:
    lines = [
        "# Written by SNAP (POP): the host's admin network to the lab router (Design 0001, D8).",
        f"auto {IFACE}",
        f"iface {IFACE} inet static",
        f"    address {ADMIN_HOST}/{ADMIN_CIDR.prefixlen}",
    ]
    for net in cfg["networks"]:
        cidr = ipaddress.IPv4Network(net["cidr"])
        lines.append(f"    up ip route replace {cidr} via {ADMIN_ROUTER} dev {IFACE}")
    return "\n".join(lines) + "\n"


def done(ctx: Context) -> bool:
    if ctx.host.read(CONFIG) != render(ctx.cfg):
        return False
    routes = ctx.host.run("ip", "-4", "route", "show", "dev", IFACE) if ctx.host.ok("ip", "link", "show", IFACE) else ""
    wanted = [str(ipaddress.IPv4Network(n["cidr"])) for n in ctx.cfg["networks"]]
    return all(f"{cidr} via {ADMIN_ROUTER}" in routes for cidr in wanted)


def apply(ctx: Context) -> None:
    main = ctx.host.read(MAIN_CONFIG) or ""
    if "source /etc/network/interfaces.d/*" not in main:
        ctx.host.write(MAIN_CONFIG, main.rstrip("\n") + "\n\nsource /etc/network/interfaces.d/*\n")
    ctx.host.write(CONFIG, render(ctx.cfg))
    ctx.host.run("ifreload", "-a")
