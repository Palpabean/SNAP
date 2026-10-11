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
        self.debs: set[str] = set()
        self.collections: dict[str, str] = {}
        self.roles: dict[str, str] = {}
        self.users: set[str] = set()
        self.acls: set[tuple[str, str, str]] = set()
        self.tokens: dict[str, str] = {}  # "user!token" -> secret value
        self.token_counter = 0
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

    def http_ok(self, url: str, headers: dict[str, str]) -> bool:
        auth = headers.get("Authorization", "")
        return any(auth == f"PVEAPIToken={tid}={value}" for tid, value in self.tokens.items())

    def _dispatch(self, cmd: tuple[str, ...]) -> str | None:
        if cmd[0] == "env":  # drop leading VAR=value assignments
            rest = list(cmd[1:])
            while rest and "=" in rest[0]:
                rest.pop(0)
            cmd = tuple(rest)
        tool, args = cmd[0], cmd[1:]
        if tool == "/usr/local/bin/tofu":
            return "OpenTofu v1.13.1\non linux_amd64\n" if self.exists(tool) else None
        if tool == "dpkg-query":
            return "install ok installed" if args[-1] in self.debs else None
        if tool == "apt-get":
            return self._apt(args)
        if tool == "ansible-galaxy":
            return self._galaxy(args)
        if tool == "pveum":
            return self._pveum(args)
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

    def _apt(self, args):
        source = next((a.split("=", 1)[1] for a in args if a.startswith("Dir::Etc::SourceList=")), None)
        if not source or not self.exists(source):
            return None
        if "update" in args:
            return ""
        if "install" in args:
            wanted = [a for a in args[args.index("install") + 1 :] if not a.startswith("-")]
            self.debs.update(wanted)
            return ""
        return None

    def _galaxy(self, args):
        if "ansible-core" not in self.debs:
            return None
        if args[:2] == ("collection", "install"):
            for archive in (a for a in args if a.endswith(".tar.gz")):
                match = re.fullmatch(r"(\w+)-(\w+)-([\d.]+)\.tar\.gz", archive.rsplit("/", 1)[-1])
                if not match or not self.exists(archive):
                    return None
                self.collections[f"{match.group(1)}.{match.group(2)}"] = match.group(3)
            return ""
        if args[:2] == ("collection", "list"):
            listing = {k: {"version": v} for k, v in self.collections.items()}
            return json.dumps({"/usr/share/ansible/collections/ansible_collections": listing})
        return None

    def _pveum(self, args):
        if args[:2] == ("role", "list"):
            return json.dumps([{"roleid": r, "privs": p} for r, p in self.roles.items()])
        if args[0] == "role" and args[1] in ("add", "modify"):
            if (args[1] == "add") == (args[2] in self.roles):
                return None
            self.roles[args[2]] = args[args.index("--privs") + 1]
            return ""
        if args[:2] == ("user", "list"):
            return json.dumps([{"userid": u} for u in sorted(self.users)])
        if args[:2] == ("user", "add"):
            self.users.add(args[2])
            return ""
        if args[:2] == ("acl", "modify"):
            self.acls.add((args[2], args[args.index("--users") + 1], args[args.index("--roles") + 1]))
            return ""
        if args[:3] == ("user", "token", "list"):
            user = args[3]
            return json.dumps([{"tokenid": t.split("!", 1)[1]} for t in self.tokens if t.startswith(user + "!")])
        if args[:3] == ("user", "token", "remove"):
            return "" if self.tokens.pop(f"{args[3]}!{args[4]}", None) is not None else None
        if args[:3] == ("user", "token", "add"):
            tid = f"{args[3]}!{args[4]}"
            if tid in self.tokens or args[3] not in self.users:
                return None
            self.token_counter += 1
            self.tokens[tid] = f"00000000-0000-0000-0000-{self.token_counter:012d}"
            return json.dumps({"full-tokenid": tid, "value": self.tokens[tid]})
        return None
