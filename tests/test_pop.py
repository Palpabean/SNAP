import bz2
import json
from pathlib import Path

import pytest
from fake_proxmox import FakeProxmox

from snaplab.core import config
from snaplab.core.engine import PROGRESS_DIR, Context, Engine
from snaplab.core.host import CommandError
from snaplab.stages.crackle import tools as crackle_tools
from snaplab.stages.pop import lab, network, opnsense
from snaplab.stages.pop import stage as pop

EXAMPLE = Path(__file__).parent.parent / "examples" / "snap.example.yaml"
PACKAGE = Path(pop.__file__).parent


@pytest.fixture
def cfg():
    return config.load(EXAMPLE)


# --- the lab model -----------------------------------------------------------


def test_machines_get_ids_macs_and_reserved_addresses(cfg):
    found = lab.machines(cfg)
    assert [m["name"] for m in found] == ["ubuntu-01", "ubuntu-02", "ubuntu-03"]
    assert [m["vmid"] for m in found] == [101, 102, 103]
    assert [m["ip"] for m in found] == ["10.10.10.100", "10.10.10.101", "10.10.10.102"]
    assert all(m["vlan"] == 10 for m in found)
    macs = [m["mac"] for m in found]
    assert len(set(macs)) == 3 and all(m.startswith("BC:24:11:") for m in macs)
    assert lab.mac("ubuntu-01") == macs[0]  # stable across runs


def test_templates_only_creates_no_machines(cfg):
    cfg["machines"]["mode"] = "templates-only"
    assert lab.machines(cfg) == []
    assert lab.verify_vars(cfg)["machines"] == []


def test_tofu_vars(cfg):
    v = lab.tofu_vars(cfg, "pve", {"opnsense": 9001, "ubuntu": 9000}, False, extra_keys=["ssh-ed25519 AAAA host"])
    assert v["router"]["lab_vlans"] == [10, 20] and v["router"]["admin_vlan"] == opnsense.ADMIN_VLAN
    assert v["router"]["started"] is False and v["router"]["template"] == 9001
    assert v["machines"][0]["template"] == 9000 and v["machines"][0]["mac"] == lab.mac("ubuntu-01")
    assert v["ssh_keys"] == ["ssh-ed25519 AAAA host"]
    assert v["password_hash"] == cfg["machines"]["password_hash"]


def rules_on(rules, iface):
    return [r for r in rules if r["interface"] == iface]


def test_policy_rules(cfg):
    rules = lab.firewall_rules(cfg)
    lab_net, services = rules_on(rules, "opt2"), rules_on(rules, "opt3")
    # Every network: no way to the admin network, DNS and ping to its own router address only.
    for found, key in ((lab_net, "opt2"), (services, "opt3")):
        assert found[0]["action"] == "block" and found[0]["destination_net"] == str(opnsense.ADMIN_CIDR)
        assert {r["destination_net"] for r in found[1:3]} == {f"{key}ip"}
    # lab: custom SSH and HTTPS to services, then internet only.
    custom = [r for r in lab_net if "port" in r["description"]]
    assert [(r["destination_net"], r["destination_port"], r["protocol"]) for r in custom] == [
        ("10.10.20.0/24", "22", "TCP"),
        ("10.10.20.0/24", "443", "TCP"),
    ]
    assert lab_net[-1]["destination_net"] == lab.PRIVATE_ALIAS and lab_net[-1]["destination_invert"] is True
    # services is isolated: nothing beyond the router.
    assert len(services) == 3
    # Rules are evaluated in sequence order, and sequences never collide.
    sequences = [r["sequence"] for r in rules]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)


def test_open_policy_and_rule_to_internet(cfg):
    cfg["networks"][1]["policy"] = "open"
    cfg["networks"][1]["rules"] = [{"action": "block", "to": "internet", "protocol": "udp", "ports": [123]}]
    services = rules_on(lab.firewall_rules(cfg), "opt3")
    blocked = services[3]
    assert blocked["action"] == "block" and blocked["destination_invert"] is True
    assert blocked["destination_port"] == "123" and blocked["protocol"] == "UDP"
    assert services[-1]["destination_net"] == "any" and services[-1]["action"] == "pass"


def test_router_vars(cfg):
    v = lab.router_vars(cfg)
    assert v["dhcp_interfaces"] == ["opt2", "opt3"]
    assert v["dhcp_subnets"][0] == {
        "interface": "opt2",
        "subnet": "10.10.10.0/24",
        "pool": "10.10.10.100 - 10.10.10.199",
        "gateway": "10.10.10.1",
        "dns": ["10.10.10.1"],
        "description": "SNAP: lab",
    }
    assert v["dhcp_reservations"][0] == {
        "ip": "10.10.10.100",
        "mac": lab.mac("ubuntu-01").lower(),
        "hostname": "ubuntu-01",
        "subnet": "10.10.10.0/24",
    }


def test_verify_expectations(cfg):
    v = lab.verify_vars(cfg)
    assert v["router"] == "10.10.10.1"
    # lab may reach services only on TCP 22 and 443, so a ping is blocked.
    assert v["expect"] == [{"network": "services", "address": "10.10.20.1", "reachable": False}]
    cfg["networks"][0]["policy"] = "open"
    assert lab.verify_vars(cfg)["expect"][0]["reachable"] is True


def test_admin_network_file(cfg):
    text = network.render(cfg)
    assert "iface vmbr1.4094 inet static" in text and "address 172.31.255.2/29" in text
    assert "up ip route replace 10.10.10.0/24 via 172.31.255.1 dev vmbr1.4094" in text


# --- the stage on a fake host ------------------------------------------------


class FakeLab(FakeProxmox):
    """FakeProxmox plus what POP uses: tofu, ansible-playbook, the router's API, and so on."""

    def __init__(self, root):
        super().__init__(root)
        self.running: set[int] = set()
        self.playbooks: list[str] = []
        self.routes: set[str] = set()
        self.write("/etc/network/interfaces", "auto lo\niface lo inet loopback\n")
        self.write("/var/run/qemu-server/.keep", "")

    def _dispatch(self, cmd):
        if cmd[0] == "env":
            rest = list(cmd[1:])
            while rest and "=" in rest[0]:
                rest.pop(0)
            cmd = tuple(rest)
        tool, args = cmd[0], cmd[1:]
        if tool == "qm" and args[0] in ("start", "stop"):
            (self.running.add if args[0] == "start" else self.running.discard)(int(args[1]))
            return ""
        if tool == "qm" and args[0] == "status" and int(args[1]) in self.vms:
            return "status: running\n" if int(args[1]) in self.running else "status: stopped\n"
        if tool == "qm" and args[0] == "create" and "import-from=" in " ".join(args):
            image = " ".join(args).split("import-from=")[1].split(",")[0]
            if not self.exists(image):
                return None
        if tool == "sh":
            return "" if args[-1].split()[-1] in ("pvesh", "pvesm", "qm", "ip", "genisoimage") else None
        if tool == "ssh-keygen":
            path = args[args.index("-f") + 1]
            self.write(path, "PRIVATE KEY\n", mode=0o600)
            self.write(path + ".pub", "ssh-ed25519 AAAAhost snap@proxmox\n")
            return ""
        if tool == "openssl":
            return "$6$salt$hashed\n"
        if tool == "genisoimage":
            self.write(args[args.index("-o") + 1], "ISO")
            return ""
        if tool == "ifreload":
            text = self.read(network.CONFIG) or ""
            self.routes = {line.split()[4] for line in text.splitlines() if "ip route replace" in line}
            return ""
        if tool == "ip" and args[:4] == ("-4", "route", "show", "dev"):
            return "".join(f"{r} via {opnsense.ADMIN_ROUTER} dev {network.IFACE}\n" for r in sorted(self.routes))
        if tool == "ip" and args[:2] == ("link", "show") and args[2] == network.IFACE:
            return "" if self.routes else None
        if tool == crackle_tools.TOFU:
            return self._tofu(args)
        if tool == "ansible-playbook":
            self.playbooks.append(args[0].rsplit("/", 1)[-1])
            return ""
        return super()._dispatch(cmd)

    def _tofu(self, args):
        workdir = args[0].removeprefix("-chdir=")
        verb = args[1]
        if verb == "init":
            self.write(f"{workdir}/.terraform.lock.hcl", "lock")
            return ""
        variables = json.loads(self.read(f"{workdir}/lab.auto.tfvars.json"))
        wanted = {variables["router"]["vmid"]: variables["router"]["name"]}
        if not any(a.startswith("-target=") for a in args):
            wanted |= {m["vmid"]: m["name"] for m in variables["machines"]}
        if verb == "plan":
            return "" if all(self.vms.get(i, {}).get("name") == n for i, n in wanted.items()) else None
        for vmid, name in wanted.items():
            if vmid not in self.vms:
                self.vms[vmid] = {"name": name, "tags": "snap"}
        if variables["router"]["started"]:
            self.running |= set(wanted)
        self.write(f"{workdir}/terraform.tfstate", "{}")
        return ""

    def http_ok(self, url, headers):
        if url == pop.API_STATUS:
            return lab.ROUTER_VMID in self.running and self.exists(pop.IMPORTED)
        return super().http_ok(url, headers)


@pytest.fixture
def host(tmp_path, cfg, monkeypatch):
    h = FakeLab(tmp_path)
    h.write(crackle_tools.API_SECRET, "snap@pve!tofu=secret\n")
    h.write(f"/var/lib/snap/{pop.OPNSENSE_ARCHIVE}", "")
    h.path(f"/var/lib/snap/{pop.OPNSENSE_ARCHIVE}").write_bytes(bz2.compress(b"nano image"))
    for name in ("tofu/main.tf", "ansible/router.yml", "ansible/verify.yml"):
        h.write(f"/var/lib/snap/lib/snaplab/stages/pop/{name}", (PACKAGE / name).read_text())
    imports = []

    class Console:
        def __init__(self, path):
            imports.append(path)

        def close(self):
            pass

    monkeypatch.setattr(opnsense, "Serial", Console)
    monkeypatch.setattr(opnsense, "run_importer", lambda console, log=None: "cd0")
    monkeypatch.setattr(pop.time, "sleep", lambda _: None)
    h.imports = imports
    h.cfg = cfg
    return h


def engine(host, messages=None):
    say = messages.append if messages is not None else (lambda _: None)
    return Engine([pop.STAGE], Context(host=host, cfg=host.cfg, node="pve", say=say))


def test_pop_builds_the_lab(host):
    messages = []
    assert engine(host, messages).resume(), messages
    assert host.exists(f"{PROGRESS_DIR}/pop.done")
    assert host.vms[pop.OPNSENSE_VMID]["template"] == "1"
    assert not host.exists(f"{pop.WORK}/opnsense-nano.img")  # the unpacked image is removed
    assert host.routes == {"10.10.10.0/24", "10.10.20.0/24"}
    assert "source /etc/network/interfaces.d/*" in host.read("/etc/network/interfaces")
    # The router: created stopped by OpenTofu, booted once with the importer answered.
    assert len(host.imports) == 1 and host.imports[0].endswith("/var/run/qemu-server/100.serial0")
    assert host.read(pop.IMPORTED).strip() == "cd0"
    assert {101, 102, 103} <= set(host.vms)
    assert host.playbooks == ["router.yml", "verify.yml"]
    # Secrets stay root-only, and the config.xml (with the hashed key) is not left lying around.
    api = json.loads(host.read(pop.API_FILE))
    assert len(api["key"]) > 40 and host.path(pop.API_FILE).stat().st_mode & 0o777 == 0o600
    assert not host.exists(f"{pop.WORK}/iso/conf/config.xml")
    tfvars = json.loads(host.read(f"{pop.TOFU_DIR}/lab.auto.tfvars.json"))
    assert tfvars["ssh_keys"] == ["ssh-ed25519 AAAAhost snap@proxmox"]


def test_second_run_changes_nothing(host):
    assert engine(host).resume()
    host.path(f"{PROGRESS_DIR}/pop.done").unlink()
    host.calls.clear()
    host.playbooks.clear()
    messages = []
    assert engine(host, messages).resume()
    assert sum("already done" in m for m in messages) == len(pop.STAGE.steps)
    assert host.playbooks == [] and len(host.imports) == 1
    assert not [c for c in host.calls if c[0] == "qm" and c[1] in ("create", "destroy", "start")]


def test_changed_rules_are_applied_again(host):
    assert engine(host).resume()
    host.path(f"{PROGRESS_DIR}/pop.done").unlink()
    host.playbooks.clear()
    host.cfg["networks"][1]["policy"] = "open"
    assert engine(host).resume()
    assert host.playbooks == ["router.yml", "verify.yml"]


def test_missed_importer_recreates_the_router(host, monkeypatch):
    def fail(console, log=None):
        raise opnsense.ImportFailed("no CD drive held the config")

    monkeypatch.setattr(opnsense, "run_importer", fail)
    assert not engine(host).resume()
    assert lab.ROUTER_VMID in host.running
    monkeypatch.setattr(opnsense, "run_importer", lambda console, log=None: "cd0")
    assert engine(host).resume()
    assert ("qm", "destroy", "100", "--purge", "1", "--destroy-unreferenced-disks", "1") in host.calls
    assert len(host.imports) == 2


def test_someone_elses_router_id_is_left_alone(host):
    host.vms[lab.ROUTER_VMID] = {"name": "my-vm"}
    assert not engine(host).resume()
    assert host.vms[lab.ROUTER_VMID] == {"name": "my-vm"}
    state = json.loads(host.read("/var/lib/snap/state.json"))
    assert "is not SNAP's" in state["stages"]["pop"]["steps"]["router"]["error"]


def test_templates_only_skips_machines_and_the_inside_check(host):
    host.cfg["machines"]["mode"] = "templates-only"
    assert engine(host).resume()
    assert not {101, 102, 103} & set(host.vms)
    assert host.playbooks == ["router.yml"]


def test_missing_image_and_files_fail_preflight(host):
    host.path(f"/var/lib/snap/{pop.OPNSENSE_ARCHIVE}").unlink()
    host.path("/var/lib/snap/lib/snaplab/stages/pop/ansible/router.yml").unlink()
    assert not engine(host).resume()
    error = json.loads(host.read("/var/lib/snap/state.json"))["stages"]["pop"]["error"]
    assert "OPNsense image is missing" in error and "ansible/router.yml is missing" in error


def test_failed_playbook_is_reported(host, monkeypatch):
    original = host._dispatch

    def dispatch(cmd):
        if "ansible-playbook" in cmd:
            raise CommandError(list(cmd), 2, "fatal: [localhost]: FAILED! => router said no")
        return original(cmd)

    monkeypatch.setattr(host, "_dispatch", dispatch)
    assert not engine(host).resume()
    state = json.loads(host.read("/var/lib/snap/state.json"))
    assert "router said no" in state["stages"]["pop"]["steps"]["router-settings"]["error"]


def test_a_lost_machine_is_recreated(host):
    assert engine(host).resume()
    del host.vms[102]
    host.path(f"{PROGRESS_DIR}/pop.done").unlink()
    assert engine(host).resume()
    assert host.vms[102]["name"] == "ubuntu-02"
