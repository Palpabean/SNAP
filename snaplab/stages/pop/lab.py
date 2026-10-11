"""The lab as concrete lists, worked out from snap.yaml (Design 0001, section 7.3, step 1).

OpenTofu and Ansible only apply what this module decides: which VMs exist
with which IDs, MAC and IP addresses, which DHCP ranges the router serves, and
which firewall rules each network's policy turns into. Keeping the decisions
here keeps them testable without any VMs.

Standard library only (runs on the Proxmox host).
"""

from __future__ import annotations

import hashlib
import ipaddress
from typing import Any

from snaplab.stages.pop.opnsense import ADMIN_CIDR, ADMIN_VLAN, interfaces

ROUTER_VMID = 100
ROUTER_NAME = "snap-router"
FIRST_MACHINE_VMID = 101
PRIVATE = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
PRIVATE_ALIAS = "snap_private"


def mac(name: str) -> str:
    """A fixed MAC address per VM name, in Proxmox's own range (BC:24:11)."""
    digest = hashlib.sha256(name.encode()).digest()
    return "BC:24:11:" + ":".join(f"{b:02X}" for b in digest[:3])


def machines(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    spec = cfg["machines"]
    if spec.get("mode", "create") != "create":
        return []
    net = next(n for n in cfg["networks"] if n["name"] == spec["network"])
    first = ipaddress.IPv4Address(net["dhcp"]["start"])
    found = []
    for i in range(spec["count"]):
        name = f"{spec['name_prefix']}-{i + 1:02d}"
        found.append(
            {
                "name": name,
                "vmid": FIRST_MACHINE_VMID + i,
                "mac": mac(name),
                "ip": str(first + i),  # reserved by the router's DHCP server
                "network": net["name"],
                "vlan": net.get("vlan", 0),
                "cpu": spec.get("cpu", 2),
                "memory_mb": spec.get("memory_mb", 2048),
                "disk_gb": spec.get("disk_gb", 20),
            }
        )
    return found


def tofu_vars(
    cfg: dict[str, Any], node: str, template: dict[str, int], router_started: bool, extra_keys: list[str] = ()
) -> dict[str, Any]:
    """lab.auto.tfvars.json for snaplab/stages/pop/tofu/main.tf."""
    from snaplab.stages.crackle.stage import LAB_BRIDGE, VM_STORAGE
    from snaplab.stages.crackle.tools import API_SECRET

    spec = cfg["machines"]
    return {
        "node": node,
        "api_token_file": API_SECRET,
        "lab_bridge": LAB_BRIDGE,
        "vm_storage": VM_STORAGE,
        "router": {
            "vmid": ROUTER_VMID,
            "name": ROUTER_NAME,
            "template": template["opnsense"],
            "config_iso": "local:iso/snap-router-config.iso",
            "started": router_started,
            "memory_mb": 1024,
            "cpu": 2,
            "uplink": "vmbr0",
            "admin_vlan": ADMIN_VLAN,
            "lab_vlans": [n.get("vlan", 0) for n in cfg["networks"]],
        },
        "machines": [
            {k: m[k] for k in ("vmid", "name", "cpu", "memory_mb", "disk_gb", "vlan", "mac")}
            | {"template": template["ubuntu"]}
            for m in machines(cfg)
        ],
        "username": spec["username"],
        "password_hash": spec.get("password_hash"),
        "ssh_keys": [*spec.get("ssh_keys", []), *extra_keys],
    }


def _rule(seq: int, iface: str, description: str, action: str = "pass", **fields: Any) -> dict[str, Any]:
    rule = {
        "sequence": seq,
        "interface": iface,
        "description": f"SNAP: {description}",
        "action": action,
        "protocol": "any",
        "destination_net": "any",
        "destination_invert": False,
        "destination_port": "",
    }
    rule.update(fields)
    return rule


def firewall_rules(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """The rules on each lab network, in order: router services, custom rules, then the policy.

    Nothing from the lab may reach the admin network (and so the Proxmox host);
    anything not passed here is blocked by OPNsense's default.
    """
    by_name = {n["name"]: n for n in cfg["networks"]}
    rules = []
    seq = 100
    for iface in interfaces(cfg):
        net = iface.network
        if net is None:
            continue
        key, name = iface.key, net["name"]
        rules.append(
            _rule(seq, key, f"{name} may not reach the admin network", "block", destination_net=str(ADMIN_CIDR))
        )  # noqa: E501
        # Only the router's own address on this network: "(self)" would also open its
        # addresses on the other networks.
        own = f"{key}ip"
        rules.append(
            _rule(
                seq + 1,
                key,
                f"{name} uses the router for DNS",
                protocol="TCP/UDP",
                destination_net=own,
                destination_port="53",
            )
        )
        rules.append(_rule(seq + 2, key, f"{name} may ping the router", protocol="ICMP", destination_net=own))
        seq += 10
        for custom in net.get("rules", []):
            to = custom["to"]
            if to == "internet":
                dest = {"destination_net": PRIVATE_ALIAS, "destination_invert": True}
            elif to in by_name:
                dest = {"destination_net": str(ipaddress.IPv4Network(by_name[to]["cidr"]))}
            else:
                dest = {"destination_net": str(ipaddress.IPv4Network(to))}
            protocol = {"tcp": "TCP", "udp": "UDP", "icmp": "ICMP", "any": "any"}[custom.get("protocol", "any")]
            text = custom.get("description") or f"{custom['action']} {name} to {to}"
            for port in custom.get("ports") or [""]:
                suffix = f" (port {port})" if port else ""
                rules.append(
                    _rule(seq, key, text + suffix, "pass" if custom["action"] == "allow" else "block",
                          protocol=protocol, destination_port=str(port), **dest)
                )  # fmt: skip
                seq += 1
        policy = net["policy"]
        if policy == "internet-only":
            rules.append(
                _rule(
                    seq, key, f"{name} may reach the internet", destination_net=PRIVATE_ALIAS, destination_invert=True
                )
            )
        elif policy == "open":
            rules.append(_rule(seq, key, f"{name} may reach the internet and the other lab networks"))
        seq = (seq // 100 + 1) * 100
    return rules


def router_vars(cfg: dict[str, Any]) -> dict[str, Any]:
    """Variables for ansible/router.yml: DHCP, reservations, aliases and rules."""
    subnets, reservations = [], []
    for iface in interfaces(cfg):
        net = iface.network
        if net is None or not net.get("dhcp", {}).get("enabled"):
            continue
        subnets.append(
            {
                "interface": iface.key,
                "subnet": str(ipaddress.IPv4Network(net["cidr"])),
                "pool": f"{net['dhcp']['start']} - {net['dhcp']['end']}",
                "gateway": net["router"],
                "dns": net.get("dns") or [net["router"]],
                "description": f"SNAP: {net['name']}",
            }
        )
    for m in machines(cfg):
        net = next(n for n in cfg["networks"] if n["name"] == m["network"])
        reservations.append(
            {"ip": m["ip"], "mac": m["mac"].lower(), "hostname": m["name"],
             "subnet": str(ipaddress.IPv4Network(net["cidr"]))}
        )  # fmt: skip
    return {
        "dhcp_interfaces": [s["interface"] for s in subnets],
        "dhcp_subnets": subnets,
        "dhcp_reservations": reservations,
        "aliases": [{"name": PRIVATE_ALIAS, "content": PRIVATE, "description": "SNAP: private address ranges"}],
        "rules": firewall_rules(cfg),
    }


def verify_vars(cfg: dict[str, Any]) -> dict[str, Any]:
    """What the check from inside the lab expects (step 6): each machine and what it may reach."""
    found = machines(cfg)
    if not found:
        return {"machines": [], "expect": []}
    home = next(n for n in cfg["networks"] if n["name"] == found[0]["network"])
    expect = []
    for other in cfg["networks"]:
        if other is home:
            continue
        allowed = home["policy"] == "open" or any(
            r["action"] == "allow" and r["to"] == other["name"] and r.get("protocol", "any") in ("any", "icmp")
            for r in home.get("rules", [])
        )
        expect.append({"network": other["name"], "address": other["router"], "reachable": allowed})
    return {"machines": [{"name": m["name"], "ip": m["ip"]} for m in found], "router": home["router"], "expect": expect}
