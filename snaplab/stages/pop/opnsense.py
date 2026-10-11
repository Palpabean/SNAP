"""The OPNsense router: its bootstrap config.xml and the unattended import (Design 0001, D2).

OPNsense's nano image runs on a serial console and, on its first boot only,
offers "Press any key to start the configuration importer". POP answers that
prompt over the VM's serial port and points the importer at a small ISO holding
conf/config.xml. The bootstrap config holds only what the API cannot do or what
is needed to reach the API: interface assignment, the admin network (D8), the
root password, and an API key. Ansible applies everything else through the API.

Standard library only (runs on the Proxmox host).
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# The admin network (D8): the Proxmox host and OPNsense meet here, on its own
# VLAN of the lab bridge. The host reaches OPNsense's API and the lab machines'
# SSH through it; nothing in the lab can open a connection back.
ADMIN_VLAN = 4094
ADMIN_CIDR = ipaddress.IPv4Network("172.31.255.0/29")
ADMIN_ROUTER = ADMIN_CIDR.network_address + 1
ADMIN_HOST = ADMIN_CIDR.network_address + 2


def admin(cfg: dict[str, Any]) -> tuple[ipaddress.IPv4Network, ipaddress.IPv4Address, ipaddress.IPv4Address]:
    """The admin network as placed for this lab (moved if it clashed): network, router, host."""
    net = ipaddress.IPv4Network(cfg.get("admin_network", str(ADMIN_CIDR)))
    return net, net.network_address + 1, net.network_address + 2


IMPORTER_PROMPT = "Press any key to start the configuration importer"
DEVICE_PROMPT = "Select device to import from"
IMPORTED = "Restoring config.xml...done."


@dataclass(frozen=True)
class Interface:
    """One OPNsense interface: config.xml name, FreeBSD device, and its network."""

    key: str  # wan, opt1, opt2, ...
    device: str  # vtnet0, vtnet1, ...
    descr: str
    network: dict[str, Any] | None  # the snap.yaml network, None for wan and admin


def interfaces(cfg: dict[str, Any]) -> list[Interface]:
    """WAN on the uplink, the admin network, then one per lab network, in snap.yaml order.

    The VM gets one network card per entry in the same order, so vtnetN matches.
    """
    found = [Interface("wan", "vtnet0", "WAN", None), Interface("opt1", "vtnet1", "ADMIN", None)]
    for i, net in enumerate(cfg["networks"]):
        found.append(Interface(f"opt{i + 2}", f"vtnet{i + 2}", net["name"].upper(), net))
    return found


def _add(parent: ET.Element, tag: str, text: str | None = None, **children: str) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = text
    for key, value in children.items():
        ET.SubElement(el, key).text = value
    return el


def _rule(filt: ET.Element, iface: str, descr: str, dest: str, protocol: str | None = None, port: str | None = None):
    rule = _add(filt, "rule", type="pass", interface=iface, ipprotocol="inet", descr=descr)
    if protocol:
        _add(rule, "protocol", protocol)
    _add(rule, "source", network=iface)
    destination = _add(rule, "destination", network=dest)
    if port:
        _add(destination, "port", port)


def render_config(cfg: dict[str, Any], api_key: str, api_secret_hash: str) -> str:
    """The bootstrap config.xml for this snap.yaml.

    api_secret_hash is the API secret as a SHA-512 crypt hash ($6$), which is
    how OPNsense stores it. The root password is the host's (its hash is $6$ or
    $5$, which both Proxmox and OPNsense accept).
    """
    host = cfg["host"]
    admin_net, admin_router, _ = admin(cfg)
    root = ET.Element("opnsense")
    _add(root, "theme", "opnsense")

    system = _add(root, "system")
    _add(system, "optimization", "normal")
    _add(system, "hostname", "router")
    _add(system, "domain", host["domain"])
    _add(system, "timezone", host["timezone"] if host["timezone"] != "UTC" else "Etc/UTC")
    _add(system, "dnsallowoverride", "1")
    group = _add(system, "group", name="admins", description="System Administrators", scope="system")
    _add(group, "gid", "1999")
    _add(group, "member", "0")
    _add(group, "priv", "page-all")
    user = _add(system, "user", name="root", descr="System Administrator", scope="system", groupname="admins")
    _add(user, "password", host["root_password_hash"])
    _add(user, "uid", "0")
    _add(user, "apikeys", f"{api_key}|{api_secret_hash}")
    _add(system, "webgui", protocol="https")
    _add(system, "disablenatreflection", "yes")
    _add(system, "usevirtualterminal", "1")
    _add(system, "serialspeed", "115200")
    _add(system, "primaryconsole", "serial")
    _add(system, "secondaryconsole", "video")
    _add(system, "ssh", group="admins")

    ifaces = _add(root, "interfaces")
    filt = _add(root, "filter")
    for iface in interfaces(cfg):
        el = _add(ifaces, iface.key, enable="1", descr=iface.descr)
        _add(el, "if", iface.device)
        if iface.key == "wan":
            _add(el, "ipaddr", "dhcp")
            _add(el, "blockbogons", "1")
        elif iface.key == "opt1":
            _add(el, "ipaddr", str(admin_router))
            _add(el, "subnet", str(admin_net.prefixlen))
            _rule(filt, "opt1", "SNAP: the Proxmox host reaches the OPNsense API", "(self)", "tcp", "443")
            _rule(filt, "opt1", "SNAP: the Proxmox host pings the router", "(self)", "icmp")
        else:
            net = iface.network
            _add(el, "ipaddr", net["router"])
            _add(el, "subnet", str(ipaddress.IPv4Network(net["cidr"]).prefixlen))
            _rule(filt, "opt1", f"SNAP: Ansible reaches {net['name']} over SSH", iface.key, "tcp", "22")
            _rule(filt, "opt1", f"SNAP: the Proxmox host pings {net['name']}", iface.key, "icmp")

    _add(root, "unbound", enable="1")
    nat = _add(root, "nat")
    _add(nat, "outbound", mode="automatic")

    ET.indent(root, space="  ")
    return '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


# --- the importer, over the serial console -----------------------------------


class ImportFailed(Exception):
    pass


class Serial:
    """A QEMU serial port exposed as a Unix socket (Proxmox: serial0: socket)."""

    def __init__(self, path: str, connect_timeout: float = 60):
        deadline = time.monotonic() + connect_timeout
        while True:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                self.sock.connect(path)
                break
            except OSError:
                self.sock.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        self.seen = ""
        self.before = ""  # the output just before the last match

    def send(self, text: str) -> None:
        self.sock.sendall(text.encode())

    def wait_for(self, *needles: str, timeout: float, log: Callable[[str], None] | None = None) -> str:
        """Read until one of needles appears in the output since the last match; return it."""
        deadline = time.monotonic() + timeout
        while True:
            for needle in needles:
                index = self.seen.find(needle)
                if index >= 0:
                    self.before = self.seen[:index]
                    self.seen = self.seen[index + len(needle) :]
                    return needle
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no {' or '.join(repr(n) for n in needles)} on the console within {timeout:.0f}s")
            self.sock.settimeout(min(left, 5))
            try:
                data = self.sock.recv(4096)
            except TimeoutError:
                continue
            if not data:
                raise ImportFailed("the console closed (the VM stopped?)")
            text = data.decode(errors="replace")
            if log:
                log(text)
            # Keep enough to match a prompt split across reads, not the whole boot.
            self.seen = (self.seen + text)[-8192:]

    def close(self) -> None:
        self.sock.close()


def cd_drives(devlist: str) -> list[str]:
    """CD drives named in the importer's `camcontrol devlist` output, e.g. ["cd0"]."""
    return re.findall(r"\((?:[a-z]+\d+,)*(cd\d+)[,)]", devlist)


IMPORT_FAILURES = ("could not be mounted", "No known partition", "could not be found")


def run_importer(console: Serial, timeout: float = 600, settle: float = 180, log=None) -> str:
    """Answer the first-boot importer prompts so config.xml is read from a CD; return the device.

    The first prompt waits only about seven seconds, so the console must be
    connected before the VM boots this far (connect right after starting it).
    The device prompt waits forever, but the CD drive may not be ready yet when
    it appears, so each candidate is tried until one imports or `settle` runs out.
    """
    console.wait_for(IMPORTER_PROMPT, timeout=timeout, log=log)
    console.send("\r")
    console.wait_for(DEVICE_PROMPT, timeout=120, log=log)
    named = cd_drives(console.before)
    candidates = named + [f"cd{i}" for i in range(4) if f"cd{i}" not in named]
    deadline = time.monotonic() + settle
    tried = []
    while True:
        for device in candidates:
            console.send(f"{device}\r")
            found = console.wait_for(IMPORTED, *IMPORT_FAILURES, timeout=120, log=log)
            if found == IMPORTED:
                return device
            tried.append(f"{device}: {found}")
            console.wait_for(DEVICE_PROMPT, timeout=60, log=log)
        if time.monotonic() > deadline:
            console.send("\r")  # leave the importer; OPNsense boots with its factory config
            raise ImportFailed("no CD drive held the config; tried " + ", ".join(tried[-len(candidates) :]))
        time.sleep(5)  # let the drives finish probing
