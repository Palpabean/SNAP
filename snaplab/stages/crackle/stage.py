"""Stage 2, CRACKLE: prepare the Proxmox host for the lab (Design 0001, section 7.2).

Uses only Proxmox's own tools (pvesh, pvesm, qm), so nothing extra has to be
installed on the host, online or offline. Every step checks the machine before
acting, so it is safe to run again after any interruption.
"""

from __future__ import annotations

import json
import re
import time

from snaplab.core.engine import Context, Stage, Step

LAB_BRIDGE = "vmbr1"
VM_STORAGE = "local-lvm"
FILE_STORAGE = "local"
# Content types the later stages need on the file storage: cloud-init snippets
# and disk image imports, on top of Proxmox's defaults.
FILE_CONTENT = {"iso", "vztmpl", "backup", "snippets", "import"}

UBUNTU_VMID = 9000
UBUNTU_NAME = "snap-ubuntu-2404"
UBUNTU_IMAGE = "images/ubuntu-24.04-server-cloudimg-amd64.img"

NO_SUBSCRIPTION = "/etc/apt/sources.list.d/pve-no-subscription.sources"
SOURCES_DIR = "/etc/apt/sources.list.d"


def _pvesh_json(ctx: Context, path: str):
    return json.loads(ctx.host.run("pvesh", "get", path, "--output-format", "json"))


# --- 1. Proxmox ready --------------------------------------------------------


def api_ready(ctx: Context) -> bool:
    return ctx.host.ok("pvesh", "get", "/version")


def wait_for_api(ctx: Context, timeout: float = 300, interval: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if api_ready(ctx):
            return
        time.sleep(interval)
    raise RuntimeError(f"the Proxmox API did not answer within {timeout:.0f} seconds")


# --- 2. package sources ------------------------------------------------------


def _codename(ctx: Context) -> str:
    text = ctx.host.read("/etc/os-release") or ""
    match = re.search(r"^VERSION_CODENAME=\"?([a-z]+)", text, re.M)
    if not match:
        raise RuntimeError("cannot tell the Debian release from /etc/os-release")
    return match.group(1)


def _enterprise_stanzas(text: str) -> list[tuple[int, str]]:
    """deb822 stanzas (index, text) that use the subscription-only enterprise repository."""
    stanzas = re.split(r"\n\s*\n", text.strip())
    return [(i, s) for i, s in enumerate(stanzas) if "enterprise.proxmox.com" in s]


def _stanza_enabled(stanza: str) -> bool:
    match = re.search(r"^Enabled:\s*(\S+)", stanza, re.M | re.I)
    return not (match and match.group(1).lower() in ("no", "false"))


def sources_done(ctx: Context) -> bool:
    if not ctx.host.exists(NO_SUBSCRIPTION):
        return False
    for path in ctx.host.glob(f"{SOURCES_DIR}/*.sources"):
        if any(_stanza_enabled(s) for _, s in _enterprise_stanzas(ctx.host.read(path) or "")):
            return False
    for path in ctx.host.glob(f"{SOURCES_DIR}/*.list"):
        for line in (ctx.host.read(path) or "").splitlines():
            if line.strip().startswith("deb") and "enterprise.proxmox.com" in line:
                return False
    return True


def set_sources(ctx: Context) -> None:
    """Use the free no-subscription repository instead of the subscription-only one.

    Offline this changes nothing until the host gets a network; online it lets
    `apt update` work without a subscription.
    """
    ctx.host.write(
        NO_SUBSCRIPTION,
        "# Added by SNAP (CRACKLE): Proxmox VE updates without a subscription.\n"
        "Types: deb\n"
        "URIs: http://download.proxmox.com/debian/pve\n"
        f"Suites: {_codename(ctx)}\n"
        "Components: pve-no-subscription\n"
        "Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n",
    )
    for path in ctx.host.glob(f"{SOURCES_DIR}/*.sources"):
        text = ctx.host.read(path) or ""
        stanzas = re.split(r"\n\s*\n", text.strip())
        changed = False
        for i, stanza in _enterprise_stanzas(text):
            if _stanza_enabled(stanza):
                stanza = re.sub(r"^Enabled:.*\n?", "", stanza, flags=re.M | re.I).rstrip("\n")
                stanzas[i] = stanza + "\nEnabled: no"
                changed = True
        if changed:
            ctx.host.write(path, "\n\n".join(stanzas) + "\n")
    for path in ctx.host.glob(f"{SOURCES_DIR}/*.list"):
        lines = (ctx.host.read(path) or "").splitlines()
        new = [
            f"# {line}" if line.strip().startswith("deb") and "enterprise.proxmox.com" in line else line
            for line in lines
        ]
        if new != lines:
            ctx.host.write(path, "\n".join(new) + "\n")


# --- 3. lab bridge -----------------------------------------------------------


def bridge_done(ctx: Context) -> bool:
    return ctx.host.ok("ip", "link", "show", LAB_BRIDGE)


def make_bridge(ctx: Context) -> None:
    """An internal, VLAN-aware bridge with no physical port: lab traffic only reaches
    the outside through the router (POP). The management connection is left alone."""
    node = f"/nodes/{ctx.node}/network"
    if not ctx.host.ok("pvesh", "get", f"{node}/{LAB_BRIDGE}"):
        ctx.host.run(
            "pvesh", "create", node,
            "--iface", LAB_BRIDGE, "--type", "bridge",
            "--bridge_vlan_aware", "1", "--autostart", "1",
            "--comments", "SNAP lab networks (internal, no physical port)",
        )  # fmt: skip
    ctx.host.run("pvesh", "set", node)  # apply the pending network change


# --- 4. storage --------------------------------------------------------------


def _file_content(ctx: Context) -> set[str]:
    content = _pvesh_json(ctx, f"/storage/{FILE_STORAGE}").get("content", "")
    return {c for c in content.split(",") if c}


def storage_done(ctx: Context) -> bool:
    return FILE_CONTENT <= _file_content(ctx)


def set_storage(ctx: Context) -> None:
    content = sorted(_file_content(ctx) | FILE_CONTENT)
    ctx.host.run("pvesm", "set", FILE_STORAGE, "--content", ",".join(content))


# --- 5. Ubuntu template ------------------------------------------------------


def _vm_config(ctx: Context, vmid: int) -> str | None:
    if not ctx.host.ok("qm", "status", str(vmid)):
        return None
    return ctx.host.run("qm", "config", str(vmid))


def template_done(ctx: Context) -> bool:
    config = _vm_config(ctx, UBUNTU_VMID)
    return config is not None and re.search(r"^template: 1$", config, re.M) is not None


def make_template(ctx: Context) -> None:
    vmid = str(UBUNTU_VMID)
    config = _vm_config(ctx, UBUNTU_VMID)
    if config is not None:
        # Left over from an interrupted attempt: start again, but never touch someone else's VM.
        if not re.search(rf"^name: {UBUNTU_NAME}$", config, re.M):
            raise RuntimeError(f"VM {vmid} already exists and is not SNAP's; free that ID and resume")
        ctx.host.run("qm", "destroy", vmid, "--purge", "1", "--destroy-unreferenced-disks", "1")
    image = f"{ctx.payload}/{UBUNTU_IMAGE}"
    ctx.host.run(
        "qm", "create", vmid,
        "--name", UBUNTU_NAME,
        "--description", "SNAP: Ubuntu 24.04 cloud image template for lab machines",
        "--tags", "snap",
        "--ostype", "l26", "--cpu", "host", "--cores", "2", "--memory", "2048",
        "--scsihw", "virtio-scsi-single",
        "--scsi0", f"{VM_STORAGE}:0,import-from={image},discard=on,ssd=1",
        "--ide2", f"{VM_STORAGE}:cloudinit",
        "--boot", "order=scsi0",
        "--net0", f"virtio,bridge={LAB_BRIDGE}",
        "--serial0", "socket", "--vga", "serial0",
        "--agent", "enabled=1",
    )  # fmt: skip
    ctx.host.run("qm", "template", vmid)


# --- stage -------------------------------------------------------------------


def preflight(ctx: Context) -> list[str]:
    problems = []
    for tool in ("pvesh", "pvesm", "qm", "ip"):
        if not ctx.host.ok("sh", "-c", f"command -v {tool}"):
            problems.append(f"{tool} is not available; is this a Proxmox VE host?")
    if not ctx.host.exists(f"{ctx.payload}/{UBUNTU_IMAGE}"):
        problems.append(f"the Ubuntu image is missing from {ctx.payload}/images; rebuild the USB stick")
    return problems


STAGE = Stage(
    name="crackle",
    title="CRACKLE (host setup)",
    preflight=preflight,
    steps=[
        Step("proxmox-ready", "Wait for Proxmox VE to be ready", api_ready, wait_for_api),
        Step("package-sources", "Use the no-subscription package sources", sources_done, set_sources),
        Step("lab-bridge", f"Create the internal lab bridge {LAB_BRIDGE}", bridge_done, make_bridge),
        Step("storage", "Enable snippets and imports on local storage", storage_done, set_storage),
        Step("ubuntu-template", "Create the Ubuntu 24.04 VM template", template_done, make_template),
    ],
)
