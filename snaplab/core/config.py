"""Load and validate snap.yaml (Design 0001, section 6).

Validation runs in two passes. The JSON Schema checks structure and the
format of each field. The checks in this module then cover rules that span
fields: addresses inside their networks, names that refer to other sections,
and networks that would collide on the internal bridge.
"""

from __future__ import annotations

import ipaddress
import json
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_VERSION = 1


class ConfigError(Exception):
    """snap.yaml could not be read or is not valid. `errors` lists every problem."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def load_schema() -> dict[str, Any]:
    text = resources.files("snaplab.core.schema").joinpath("snap.schema.json").read_text()
    return json.loads(text)


def load(path: str | Path) -> dict[str, Any]:
    """Read and validate a snap.yaml file. Raises ConfigError listing every problem."""
    try:
        data = yaml.safe_load(Path(path).read_text())
    except OSError as e:
        raise ConfigError([f"cannot read {path}: {e.strerror}"]) from e
    except yaml.YAMLError as e:
        raise ConfigError([f"{path} is not valid YAML: {e}"]) from e
    errors = validate(data)
    if errors:
        raise ConfigError(errors)
    return data


def validate(data: Any) -> list[str]:
    """Return every problem with `data`, or an empty list if it is valid."""
    validator = Draft202012Validator(load_schema(), format_checker=FormatChecker())
    errors = [
        f"{_path(e.absolute_path)}: {e.message}"
        for e in sorted(validator.iter_errors(data), key=lambda e: list(map(str, e.absolute_path)))
    ]
    # Cross-field checks assume the structure is sound.
    if errors:
        return errors
    return _check_host(data["host"]) + _check_networks(data["networks"]) + _check_machines(data)


def _path(parts) -> str:
    out = ""
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else p)
    return out or "(top level)"


def _check_host(host: dict[str, Any]) -> list[str]:
    mgmt = host["management"]
    if mgmt["mode"] != "static":
        return []
    iface = ipaddress.IPv4Interface(mgmt["address"])
    if ipaddress.IPv4Address(mgmt["gateway"]) not in iface.network:
        return [f"host.management.gateway: {mgmt['gateway']} is not inside {iface.network}"]
    return []


def _check_networks(networks: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen_names: set[str] = set()
    seen_vlans: dict[int, str] = {}
    parsed: list[tuple[str, ipaddress.IPv4Network]] = []
    untagged: list[str] = []

    for i, net in enumerate(networks):
        where = f"networks[{i}]"
        name = net["name"]
        if name in seen_names:
            errors.append(f"{where}.name: '{name}' is used by more than one network")
        if name == "internet":
            errors.append(f"{where}.name: 'internet' is reserved for rules")
        seen_names.add(name)

        if "vlan" in net:
            if net["vlan"] in seen_vlans:
                errors.append(f"{where}.vlan: {net['vlan']} is already used by '{seen_vlans[net['vlan']]}'")
            seen_vlans[net["vlan"]] = name
        else:
            untagged.append(name)

        try:
            cidr = ipaddress.IPv4Network(net["cidr"])
        except ValueError as e:
            errors.append(f"{where}.cidr: {e}")
            continue
        if cidr.prefixlen > 29:
            errors.append(f"{where}.cidr: /{cidr.prefixlen} is too small for a router and machines; use /29 or larger")
            continue
        parsed.append((name, cidr))

        hosts = (cidr.network_address, cidr.broadcast_address)
        router = ipaddress.IPv4Address(net["router"])
        if router not in cidr or router in hosts:
            errors.append(f"{where}.router: {router} is not a usable address in {cidr}")

        dhcp = net.get("dhcp", {})
        if dhcp.get("enabled"):
            start, end = ipaddress.IPv4Address(dhcp["start"]), ipaddress.IPv4Address(dhcp["end"])
            for key, addr in (("start", start), ("end", end)):
                if addr not in cidr or addr in hosts:
                    errors.append(f"{where}.dhcp.{key}: {addr} is not a usable address in {cidr}")
            if start > end:
                errors.append(f"{where}.dhcp: start {start} is after end {end}")
            elif start <= router <= end:
                errors.append(f"{where}.dhcp: the range {start}-{end} contains the router address {router}")

    if len(untagged) > 1:
        errors.append(
            "networks: only one network can be untagged on the internal bridge; "
            f"give a vlan to all but one of: {', '.join(untagged)}"
        )

    for a in range(len(parsed)):
        for b in range(a + 1, len(parsed)):
            (name_a, net_a), (name_b, net_b) = parsed[a], parsed[b]
            if net_a.overlaps(net_b):
                errors.append(f"networks: '{name_a}' ({net_a}) overlaps '{name_b}' ({net_b})")

    for i, net in enumerate(networks):
        for j, rule in enumerate(net.get("rules", [])):
            where = f"networks[{i}].rules[{j}]"
            to = rule["to"]
            if to == "internet" or to in seen_names:
                if to == net["name"]:
                    errors.append(f"{where}.to: a network cannot have a rule to itself")
            else:
                try:
                    ipaddress.IPv4Network(to)
                except ValueError:
                    errors.append(f"{where}.to: '{to}' is not a network name, 'internet', or a CIDR")
            if "ports" in rule and rule.get("protocol", "any") not in ("tcp", "udp"):
                errors.append(f"{where}.ports: ports need protocol tcp or udp")
    return errors


def _check_machines(data: dict[str, Any]) -> list[str]:
    machines = data["machines"]
    by_name = {n["name"]: n for n in data["networks"]}
    net = by_name.get(machines["network"])
    if net is None:
        return [f"machines.network: no network is named '{machines['network']}'"]
    if machines.get("mode", "create") != "create" or machines["count"] == 0:
        return []
    dhcp = net.get("dhcp", {})
    if not dhcp.get("enabled"):
        return [f"machines.network: '{net['name']}' needs DHCP enabled for the machines to get addresses"]
    size = int(ipaddress.IPv4Address(dhcp["end"])) - int(ipaddress.IPv4Address(dhcp["start"])) + 1
    if size > 0 and machines["count"] > size:
        count, name = machines["count"], net["name"]
        return [f"machines.count: {count} machines do not fit in the {size}-address DHCP range of '{name}'"]
    return []
