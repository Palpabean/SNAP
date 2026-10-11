"""Stage 3, POP: the router, the lab networks and the lab machines (Design 0001, section 7.3).

OpenTofu creates the VMs (D7) and Ansible configures the router through its
API and checks the lab from inside. The router's first boot is the one thing
done by hand-over-console: POP answers OPNsense's config importer over the
serial port (D2). Every step checks the machine first, so it is safe to repeat.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time

from snaplab.core.engine import Context, Stage, Step
from snaplab.stages.crackle import tools as crackle_tools
from snaplab.stages.crackle.stage import UBUNTU_VMID, VM_STORAGE
from snaplab.stages.pop import lab, network, opnsense

OPNSENSE_VMID = 9001
OPNSENSE_NAME = "snap-opnsense-261"
OPNSENSE_ARCHIVE = "images/opnsense-nano-amd64.img.bz2"

WORK = "/var/lib/snap/pop"
TOFU_DIR = f"{WORK}/tofu"
ANSIBLE_DIR = f"{WORK}/ansible"
API_FILE = "/var/lib/snap/secrets/opnsense-api.json"
SSH_KEY = "/root/.ssh/snap_lab_ed25519"
CONFIG_ISO = "/var/lib/vz/template/iso/snap-router-config.iso"
IMPORTED = f"{WORK}/router-imported"
CONSOLE_LOG = "/var/log/snap/router-console.log"
API_STATUS = f"https://{opnsense.ADMIN_ROUTER}/api/core/firmware/status"

# Run the tools as root with a predictable environment: the resume service has no HOME.
ENV = [
    "env", "HOME=/root", "TF_IN_AUTOMATION=1", f"TF_CLI_CONFIG_FILE={crackle_tools.TOFU_CONFIG}",
    "ANSIBLE_HOST_KEY_CHECKING=False", "ANSIBLE_NOCOLOR=1", "ANSIBLE_RETRY_FILES_ENABLED=False",
]  # fmt: skip


def _source(ctx: Context, *parts: str) -> str:
    """A file shipped with the engine on the stick, e.g. stages/pop/ansible/router.yml."""
    return "/".join([ctx.payload, "lib", "snaplab", *parts])


def _vm_config(ctx: Context, vmid: int) -> str | None:
    if not ctx.host.ok("qm", "status", str(vmid)):
        return None
    return ctx.host.run("qm", "config", str(vmid))


def _digest(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


# --- 1. OPNsense template ----------------------------------------------------


def template_done(ctx: Context) -> bool:
    config = _vm_config(ctx, OPNSENSE_VMID)
    return config is not None and re.search(r"^template: 1$", config, re.M) is not None


def make_template(ctx: Context) -> None:
    """The nano image as template VM 9001: BIOS boot, serial console, one card on the uplink."""
    vmid = str(OPNSENSE_VMID)
    config = _vm_config(ctx, OPNSENSE_VMID)
    if config is not None:
        if not re.search(rf"^name: {OPNSENSE_NAME}$", config, re.M):
            raise RuntimeError(f"VM {vmid} already exists and is not SNAP's; free that ID and resume")
        ctx.host.run("qm", "destroy", vmid, "--purge", "1", "--destroy-unreferenced-disks", "1")
    raw = f"{WORK}/opnsense-nano.img"
    ctx.host.decompress_bz2(f"{ctx.payload}/{OPNSENSE_ARCHIVE}", raw)
    try:
        ctx.host.run(
            "qm", "create", vmid,
            "--name", OPNSENSE_NAME,
            "--description", "SNAP: OPNsense 26.1 (nano image) template for the lab router",
            "--tags", "snap",
            "--ostype", "other", "--cpu", "host", "--cores", "2", "--memory", "1024",
            "--scsihw", "virtio-scsi-single",
            "--scsi0", f"{VM_STORAGE}:0,import-from={raw},discard=on",
            "--boot", "order=scsi0",
            "--net0", "virtio,bridge=vmbr0",
            "--serial0", "socket",
        )  # fmt: skip
        ctx.host.run("qm", "template", vmid)
    finally:
        ctx.host.remove(raw)


# --- 2. the host's admin network (D8) is in network.py ------------------------

# --- 3. SSH key for Ansible --------------------------------------------------


def ssh_key_done(ctx: Context) -> bool:
    return ctx.host.exists(SSH_KEY) and ctx.host.exists(SSH_KEY + ".pub")


def make_ssh_key(ctx: Context) -> None:
    ctx.host.mkdir("/root/.ssh", mode=0o700)
    ctx.host.remove(SSH_KEY)
    ctx.host.remove(SSH_KEY + ".pub")
    ctx.host.run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "snap@proxmox", "-f", SSH_KEY)


# --- 4. the router's bootstrap config ----------------------------------------


def _api(ctx: Context) -> dict | None:
    text = ctx.host.read(API_FILE)
    return json.loads(text) if text else None


def router_config_done(ctx: Context) -> bool:
    return _api(ctx) is not None and ctx.host.exists(CONFIG_ISO)


def _make_iso(ctx: Context, source: str, out: str) -> None:
    for tool in (["genisoimage"], ["xorriso", "-as", "mkisofs"], ["mkisofs"]):
        if ctx.host.ok("sh", "-c", f"command -v {tool[0]}"):
            ctx.host.run(*tool, "-quiet", "-r", "-V", "SNAPCONF", "-o", out, source)
            return
    raise RuntimeError("no ISO tool (genisoimage or xorriso) found; Proxmox normally has genisoimage")


def make_router_config(ctx: Context) -> None:
    """An API key (secret kept root-only) and config.xml on a small ISO for the importer."""
    api = _api(ctx)
    if api is None:
        api = {"key": secrets.token_urlsafe(60), "secret": secrets.token_urlsafe(60)}
        ctx.host.mkdir("/var/lib/snap/secrets", mode=0o700)
        ctx.host.write(API_FILE, json.dumps(api) + "\n", mode=0o600)
    hashed = ctx.host.run("openssl", "passwd", "-6", "-stdin", input=api["secret"]).strip()
    staging = f"{WORK}/iso"
    ctx.host.mkdir(f"{staging}/conf", mode=0o700)
    ctx.host.write(f"{staging}/conf/config.xml", opnsense.render_config(ctx.cfg, api["key"], hashed), mode=0o600)
    ctx.host.mkdir("/var/lib/vz/template/iso")
    _make_iso(ctx, f"{staging}", CONFIG_ISO)
    ctx.host.remove(f"{staging}/conf/config.xml")


# --- 5. the router VM and its first boot --------------------------------------


def _templates() -> dict[str, int]:
    return {"opnsense": OPNSENSE_VMID, "ubuntu": UBUNTU_VMID}


def _ssh_keys(ctx: Context) -> list[str]:
    return [(ctx.host.read(SSH_KEY + ".pub") or "").strip()]


def _tofu(ctx: Context, router_started: bool, *args: str) -> None:
    """Write the configuration and variables, initialise (offline), then run tofu with args."""
    ctx.host.mkdir(TOFU_DIR, mode=0o700)
    ctx.host.write(f"{TOFU_DIR}/main.tf", ctx.host.read(_source(ctx, "stages", "pop", "tofu", "main.tf")) or "")
    variables = lab.tofu_vars(ctx.cfg, ctx.node, _templates(), router_started, extra_keys=_ssh_keys(ctx))
    ctx.host.write(f"{TOFU_DIR}/lab.auto.tfvars.json", json.dumps(variables, indent=2) + "\n", mode=0o600)
    tofu = [*ENV, crackle_tools.TOFU, f"-chdir={TOFU_DIR}"]
    if not ctx.host.exists(f"{TOFU_DIR}/.terraform.lock.hcl"):
        ctx.host.run(*tofu, "init", "-input=false")
    ctx.host.run(*tofu, *args)


def router_ready(ctx: Context) -> bool:
    api = _api(ctx)
    if api is None:
        return False
    token = base64.b64encode(f"{api['key']}:{api['secret']}".encode()).decode()
    return ctx.host.http_ok(API_STATUS, {"Authorization": f"Basic {token}"})


def wait_for_router(ctx: Context, timeout: float = 900, interval: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not router_ready(ctx):
        if time.monotonic() > deadline:
            raise RuntimeError(f"the router's API did not answer within {timeout:.0f} seconds")
        time.sleep(interval)


def make_router(ctx: Context) -> None:
    """Clone the router, boot it once while answering the config importer, wait for its API.

    The importer is offered on the very first boot only. If that boot was missed
    (an interruption), the router is recreated rather than left half set up.
    """
    vmid = str(lab.ROUTER_VMID)
    if ctx.host.exists(IMPORTED):
        if "status: running" not in ctx.host.run("qm", "status", vmid):
            ctx.host.run("qm", "start", vmid)
        wait_for_router(ctx)
        return
    config = _vm_config(ctx, lab.ROUTER_VMID)
    if config is not None:
        if not re.search(rf"^name: {lab.ROUTER_NAME}$", config, re.M):
            raise RuntimeError(f"VM {vmid} already exists and is not SNAP's; free that ID and resume")
        if "status: running" in ctx.host.run("qm", "status", vmid):
            ctx.host.run("qm", "stop", vmid)
        ctx.host.run("qm", "destroy", vmid, "--purge", "1", "--destroy-unreferenced-disks", "1")
        ctx.host.remove(f"{TOFU_DIR}/terraform.tfstate")
    _tofu(ctx, False, "apply", "-input=false", "-auto-approve", "-target=proxmox_virtual_environment_vm.router")
    ctx.host.run("qm", "start", vmid)
    ctx.say("Router: answering the OPNsense config importer on its serial console")
    console = opnsense.Serial(str(ctx.host.path(f"/var/run/qemu-server/{vmid}.serial0")))
    try:
        device = opnsense.run_importer(console, log=lambda text: ctx.host.append(CONSOLE_LOG, text))
    finally:
        console.close()
    ctx.host.write(IMPORTED, f"{device}\n")
    wait_for_router(ctx)


# --- 6. router settings through the API (Ansible) ----------------------------


def _playbook(ctx: Context, name: str, *args: str) -> None:
    ctx.host.mkdir(ANSIBLE_DIR, mode=0o700)
    playbook = f"{ANSIBLE_DIR}/{name}"
    ctx.host.write(playbook, ctx.host.read(_source(ctx, "stages", "pop", "ansible", name)) or "")
    ctx.host.run(*ENV, "ansible-playbook", playbook, *args)


def _router_vars(ctx: Context) -> dict:
    return lab.router_vars(ctx.cfg)


def router_settings_done(ctx: Context) -> bool:
    return (ctx.host.read(f"{WORK}/router-settings.sha256") or "").strip() == _digest(_router_vars(ctx))


def apply_router_settings(ctx: Context) -> None:
    variables = _router_vars(ctx)
    api = _api(ctx)
    credentials = {
        "opnsense_host": str(opnsense.ADMIN_ROUTER),
        "opnsense_key": api["key"],
        "opnsense_secret": api["secret"],
    }
    ctx.host.mkdir(ANSIBLE_DIR, mode=0o700)
    ctx.host.write(f"{ANSIBLE_DIR}/router.json", json.dumps(variables, indent=2) + "\n", mode=0o600)
    ctx.host.write(f"{ANSIBLE_DIR}/api.json", json.dumps(credentials) + "\n", mode=0o600)
    _playbook(ctx, "router.yml", "-e", f"@{ANSIBLE_DIR}/router.json", "-e", f"@{ANSIBLE_DIR}/api.json")
    ctx.host.write(f"{WORK}/router-settings.sha256", _digest(variables) + "\n")


# --- 7. lab machines (OpenTofu) ----------------------------------------------


def _tofu_digest(ctx: Context) -> str:
    variables = lab.tofu_vars(ctx.cfg, ctx.node, _templates(), True, extra_keys=_ssh_keys(ctx))
    main = ctx.host.read(_source(ctx, "stages", "pop", "tofu", "main.tf")) or ""
    return _digest([variables, main])


def machines_done(ctx: Context) -> bool:
    """Applied with the current settings, and every lab machine exists.

    Not `tofu plan`: providers can report harmless differences forever, and
    the step would never count as done.
    """
    if (ctx.host.read(f"{WORK}/machines.sha256") or "").strip() != _tofu_digest(ctx):
        return False
    return all(_vm_config(ctx, m["vmid"]) is not None for m in lab.machines(ctx.cfg))


def make_machines(ctx: Context) -> None:
    _tofu(ctx, True, "apply", "-input=false", "-auto-approve")
    ctx.host.write(f"{WORK}/machines.sha256", _tofu_digest(ctx) + "\n")


# --- 8. check the lab from inside --------------------------------------------


def _verify_vars(ctx: Context) -> dict:
    return lab.verify_vars(ctx.cfg) | {"domain": ctx.cfg["host"]["domain"]}


def _verified_digest(ctx: Context, variables: dict) -> str:
    # Check again whenever the machines or the router's settings change.
    return _digest([variables, _router_vars(ctx)])


def verify_done(ctx: Context) -> bool:
    variables = _verify_vars(ctx)
    if not variables["machines"]:
        return True  # templates only: nothing to check from inside
    return (ctx.host.read(f"{WORK}/verified.sha256") or "").strip() == _verified_digest(ctx, variables)


def verify(ctx: Context) -> None:
    variables = _verify_vars(ctx)
    user = ctx.cfg["machines"]["username"]
    inventory = ["[lab]"] + [
        f"{m['name']} ansible_host={m['ip']} ansible_user={user} ansible_ssh_private_key_file={SSH_KEY}"
        for m in variables["machines"]
    ]
    ctx.host.mkdir(ANSIBLE_DIR, mode=0o700)
    ctx.host.write(f"{ANSIBLE_DIR}/inventory.ini", "\n".join(inventory) + "\n")
    ctx.host.write(f"{ANSIBLE_DIR}/verify.json", json.dumps(variables, indent=2) + "\n")
    _playbook(ctx, "verify.yml", "-i", f"{ANSIBLE_DIR}/inventory.ini", "-e", f"@{ANSIBLE_DIR}/verify.json")
    ctx.host.write(f"{WORK}/verified.sha256", _verified_digest(ctx, variables) + "\n")


# --- stage -------------------------------------------------------------------


def preflight(ctx: Context) -> list[str]:
    problems = []
    if not ctx.host.exists(f"{ctx.payload}/{OPNSENSE_ARCHIVE}") and not template_done(ctx):
        problems.append(f"the OPNsense image is missing from {ctx.payload}/images; rebuild the USB stick")
    for name in (("tofu", "main.tf"), ("ansible", "router.yml"), ("ansible", "verify.yml")):
        if not ctx.host.exists(_source(ctx, "stages", "pop", *name)):
            problems.append(f"{'/'.join(name)} is missing from the engine; rebuild the USB stick")
    if not ctx.host.exists(crackle_tools.API_SECRET):
        problems.append("the OpenTofu API token is missing; run CRACKLE again")
    return problems


STAGE = Stage(
    name="pop",
    title="POP (router, networks and lab machines)",
    preflight=preflight,
    steps=[
        Step("opnsense-template", "Create the OPNsense router template", template_done, make_template),
        Step("admin-network", f"Connect the host to the admin network ({network.IFACE})", network.done, network.apply),
        Step("ssh-key", "Create the SSH key Ansible uses", ssh_key_done, make_ssh_key),
        Step("router-config", "Prepare the router's bootstrap config", router_config_done, make_router_config),
        Step("router", "Create the router and import its config", router_ready, make_router),
        Step("router-settings", "Set up DHCP and the firewall rules", router_settings_done, apply_router_settings),
        Step("machines", "Create the lab machines with OpenTofu", machines_done, make_machines),
        Step("verify", "Check the lab from inside", verify_done, verify),
    ],
)
