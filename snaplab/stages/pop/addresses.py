"""Keep the lab's addresses clear of the networks the host is already on.

snap.yaml (or SNAP's defaults) picks the lab's address ranges before anyone
knows the home network the computer will be plugged into. If a lab network or
the admin network (D8) overlaps a network the host can already see, the lab
could not reach the internet, or the host could not reach the router. So when
POP starts, each clashing range moves to the next free range of the same size,
with its router address, DHCP range and rules moved along. The result is
saved, so later runs never move addresses under an existing router.

Standard library only (runs on the Proxmox host).
"""

from __future__ import annotations

import copy
import ipaddress
import json
from collections.abc import Iterator
from typing import Any

from snaplab.stages.pop.opnsense import ADMIN_CIDR

Net = ipaddress.IPv4Network
PRIVATE_BLOCKS = [Net("10.0.0.0/8"), Net("172.16.0.0/12"), Net("192.168.0.0/16")]
LAB_BRIDGE = "vmbr1"


def taken(addr_json: str, route_json: str) -> list[Net]:
    """Networks the host is on or routes to, from `ip -j -4 addr` and `ip -j -4 route`.

    The lab bridge (vmbr1 and its VLANs) is SNAP's own and is left out.
    """
    found: set[Net] = set()
    for link in json.loads(addr_json or "[]"):
        name = link.get("ifname", "")
        if name == "lo" or name.startswith(LAB_BRIDGE):
            continue
        for a in link.get("addr_info", []):
            if a.get("family") == "inet":
                found.add(ipaddress.IPv4Interface(f"{a['local']}/{a['prefixlen']}").network)
    for route in json.loads(route_json or "[]"):
        dst, dev = route.get("dst", ""), route.get("dev", "")
        if dst in ("", "default") or dev.startswith(LAB_BRIDGE):
            continue
        net = Net(dst if "/" in dst else f"{dst}/32", strict=False)
        if net.prefixlen > 0:
            found.add(net)
    return sorted(found)


def _candidates(original: Net) -> Iterator[Net]:
    """Same-size ranges after `original` in its private block (wrapping round), then in the others."""
    home = next((b for b in PRIVATE_BLOCKS if original.subnet_of(b)), PRIVATE_BLOCKS[0])
    for block in [home] + [b for b in PRIVATE_BLOCKS if b != home]:
        if original.prefixlen < block.prefixlen:
            continue
        size = original.num_addresses
        count = block.num_addresses // size
        start = (int(original.network_address) - int(block.network_address)) // size + 1 if block == home else 0
        for i in range(count):
            first = int(block.network_address) + ((start + i) % count) * size
            yield Net(f"{ipaddress.IPv4Address(first)}/{original.prefixlen}")


def plan(cfg: dict[str, Any], busy: list[Net]) -> dict[str, Any]:
    """Where each lab network and the admin network go.

    Ranges that do not clash with the host's networks stay where they are;
    only the clashing ones move, each to the next free range of its size.
    """
    wanted = [(n["name"], Net(n["cidr"])) for n in cfg["networks"]] + [("admin", ADMIN_CIDR)]
    result = {name: net for name, net in wanted if not any(net.overlaps(b) for b in busy)}
    if "admin" in result and any(result["admin"].overlaps(n) for k, n in result.items() if k != "admin"):
        del result["admin"]  # a lab network uses the admin range: the admin network moves
    used = list(busy) + list(result.values())
    for name, net in wanted:
        if name in result:
            continue
        new = next((c for c in _candidates(net) if not any(c.overlaps(u) for u in used)), None)
        if new is None:
            raise RuntimeError(f"no free address range for the {name} network")
        used.append(new)
        result[name] = new
    placed = {name: str(result[name]) for name, _ in wanted}
    admin = placed.pop("admin")
    return {"networks": placed, "admin": admin}


def _move(address: str, old: Net, new: Net) -> str:
    return str(new.network_address + (int(ipaddress.IPv4Address(address)) - int(old.network_address)))


def apply(cfg: dict[str, Any], placed: dict[str, Any]) -> dict[str, Any]:
    """A copy of cfg with each network moved to its planned range (router, DHCP and rules too)."""
    out = copy.deepcopy(cfg)
    moves = {}
    for net in out["networks"]:
        old, new = Net(net["cidr"]), Net(placed["networks"][net["name"]])
        if old == new:
            continue
        moves[old] = new
        net["cidr"] = str(new)
        net["router"] = _move(net["router"], old, new)
        dhcp = net.get("dhcp", {})
        for key in ("start", "end"):
            if key in dhcp:
                dhcp[key] = _move(dhcp[key], old, new)
        net["dns"] = [_move(d, old, new) if ipaddress.IPv4Address(d) in old else d for d in net.get("dns", [])]
        if not net["dns"]:
            del net["dns"]
    for net in out["networks"]:
        for rule in net.get("rules", []):
            try:
                target = Net(rule["to"])
            except ValueError:
                continue  # a network name or "internet"
            if target in moves:
                rule["to"] = str(moves[target])
    out["admin_network"] = placed["admin"]
    return out


def describe(cfg: dict[str, Any], placed: dict[str, Any]) -> list[str]:
    lines = [
        f"lab network '{n['name']}' overlaps a network this computer is on; using {placed['networks'][n['name']]} "
        f"instead of {n['cidr']}"
        for n in cfg["networks"]
        if Net(n["cidr"]) != Net(placed["networks"][n["name"]])
    ]
    if Net(placed["admin"]) != ADMIN_CIDR:
        lines.append(f"the admin network {ADMIN_CIDR} is taken; using {placed['admin']}")
    return lines


# --- the POP step ------------------------------------------------------------

PLAN_FILE = "/var/lib/snap/pop/addresses.json"


def _wanted(cfg: dict[str, Any]) -> list[list[str]]:
    return [[n["name"], n["cidr"]] for n in cfg["networks"]]


def lab_cfg(ctx) -> dict[str, Any]:
    """snap.yaml with the lab's addresses as placed on this host; the other POP steps use this."""
    text = ctx.host.read(PLAN_FILE)
    if not text:
        raise RuntimeError("the lab's addresses are not chosen yet")
    return apply(ctx.cfg, json.loads(text)["placed"])


def done(ctx) -> bool:
    text = ctx.host.read(PLAN_FILE)
    return bool(text) and json.loads(text).get("for") == _wanted(ctx.cfg)


def choose(ctx) -> None:
    busy = taken(ctx.host.run("ip", "-j", "-4", "addr", "show"), ctx.host.run("ip", "-j", "-4", "route", "show"))
    placed = plan(ctx.cfg, busy)
    for line in describe(ctx.cfg, placed):
        ctx.say(f"Addresses: {line}")
    record = {"for": _wanted(ctx.cfg), "busy": [str(b) for b in busy], "placed": placed}
    ctx.host.write(PLAN_FILE, json.dumps(record, indent=2) + "\n")
