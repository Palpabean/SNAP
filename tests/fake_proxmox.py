"""A fake Proxmox VE host for engine and stage tests.

It answers the pvesh, pvesm, qm and ip commands CRACKLE uses and keeps their
state in memory; files live under a temporary root directory.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from snaplab.core.host import CommandError, Host

ENTERPRISE_SOURCES = """Types: deb
URIs: https://enterprise.proxmox.com/debian/pve
Suites: trixie
Components: pve-enterprise
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
"""

CEPH_SOURCES = """Types: deb
URIs: https://enterprise.proxmox.com/debian/ceph-squid
Suites: trixie
Components: enterprise
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
"""


class FakeProxmox(Host):
    def __init__(self, root: Path):
        super().__init__(root)
        self.calls: list[tuple[str, ...]] = []
        self.api_up = True
        self.bridges = {"vmbr0"}
        self.pending_bridges: set[str] = set()
        self.local_content = {"iso", "vztmpl", "backup"}
        self.vms: dict[int, dict[str, str]] = {}
        self.fail: dict[str, str] = {}  # command word -> error, to inject failures
        self.write("/etc/os-release", 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nVERSION_CODENAME=trixie\n')
        self.write("/etc/apt/sources.list.d/pve-enterprise.sources", ENTERPRISE_SOURCES)
        self.write("/etc/apt/sources.list.d/ceph.sources", CEPH_SOURCES)
        self.write("/etc/apt/sources.list.d/debian.sources", "Types: deb\nURIs: http://deb.debian.org/debian\n")

    # --- commands ------------------------------------------------------------

    def run(self, *cmd: str, input: str | None = None) -> str:
        self.calls.append(cmd)
        for word, error in self.fail.items():
            if word in cmd:
                raise CommandError(list(cmd), 1, error)
        out = self._dispatch(cmd)
        if out is None:
            raise CommandError(list(cmd), 2, f"fake: unhandled or failing command {cmd}")
        return out

    def ok(self, *cmd: str) -> bool:
        try:
            self.run(*cmd)
            return True
        except CommandError:
            return False

    def _dispatch(self, cmd: tuple[str, ...]) -> str | None:
        tool, args = cmd[0], cmd[1:]
        if tool == "sh":  # command -v <tool>
            return "" if args[-1].split()[-1] in ("pvesh", "pvesm", "qm", "ip") else None
        if tool == "ip":
            return "" if args[:2] == ("link", "show") and args[2] in self.bridges else None
        if tool == "pvesh":
            return self._pvesh(args)
        if tool == "pvesm":
            if args[:2] == ("set", "local") and args[2] == "--content":
                self.local_content = set(args[3].split(","))
                return ""
            return None
        if tool == "qm":
            return self._qm(args)
        return None

    def _pvesh(self, args):
        verb, path = args[0], args[1]
        if path == "/version":
            return json.dumps({"version": "9.2.1"}) if self.api_up else None
        if verb == "get" and path == "/storage/local":
            return json.dumps({"storage": "local", "content": ",".join(sorted(self.local_content))})
        match = re.fullmatch(r"/nodes/[^/]+/network(?:/(\w+))?", path)
        if match:
            iface = match.group(1)
            if verb == "get" and iface:
                return "{}" if iface in self.bridges | self.pending_bridges else None
            if verb == "create":
                self.pending_bridges.add(args[args.index("--iface") + 1])
                return ""
            if verb == "set":  # apply pending network changes
                self.bridges |= self.pending_bridges
                self.pending_bridges.clear()
                return ""
        return None

    def _qm(self, args):
        verb, vmid = args[0], int(args[1])
        opts = dict(zip(args[2::2], args[3::2], strict=False))
        if verb == "status":
            return "status: stopped\n" if vmid in self.vms else None
        if verb == "config":
            vm = self.vms.get(vmid)
            return None if vm is None else "".join(f"{k}: {v}\n" for k, v in vm.items())
        if verb == "create":
            if vmid in self.vms:
                return None
            image = re.search(r"import-from=([^,]+)", opts.get("--scsi0", ""))
            if image and not self.exists(image.group(1)):
                return None
            self.vms[vmid] = {k.lstrip("-"): v for k, v in opts.items()}
            return ""
        if verb == "template":
            self.vms[vmid]["template"] = "1"
            return ""
        if verb == "destroy":
            return "" if self.vms.pop(vmid, None) is not None else None
        return None
