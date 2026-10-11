"""CRACKLE steps for the automation tools (design decision D7).

OpenTofu (with the bpg/proxmox provider) provisions the lab; Ansible (with its
collections) configures it. Both are installed offline from what the stick
brought (tools.json lists it), and OpenTofu gets a least-privilege Proxmox API
token. Every step checks the machine first, so it is safe to repeat.
"""

from __future__ import annotations

import json
import re

from snaplab.core.engine import Context, Step
from snaplab.core.host import CommandError

TOFU = "/usr/local/bin/tofu"
PROVIDER_MIRROR = "/usr/local/share/snap/tofu-providers"
TOFU_CONFIG = "/root/.tofurc"
COLLECTIONS = "/usr/share/ansible/collections"

API_USER = "snap@pve"
API_TOKEN = "tofu"
API_ROLE = "SnapAutomation"
API_SECRET = "/var/lib/snap/secrets/proxmox-api-token"
API_URL = "https://127.0.0.1:8006/api2/json/version"
# What OpenTofu needs to clone the template into lab VMs and manage them, and no more.
API_PRIVILEGES = [
    "Datastore.AllocateSpace", "Datastore.Audit", "SDN.Use", "Sys.Audit",
    "VM.Allocate", "VM.Audit", "VM.Clone", "VM.PowerMgmt", "VM.GuestAgent.Audit",
    "VM.Config.CDROM", "VM.Config.CPU", "VM.Config.Cloudinit", "VM.Config.Disk",
    "VM.Config.HWType", "VM.Config.Memory", "VM.Config.Network", "VM.Config.Options",
]  # fmt: skip


def tools(ctx: Context) -> dict:
    text = ctx.host.read(f"{ctx.payload}/tools.json")
    if not text:
        raise RuntimeError("tools.json is missing from /var/lib/snap; rebuild the USB stick")
    return json.loads(text)


def preflight(ctx: Context) -> list[str]:
    try:
        spec = tools(ctx)
    except RuntimeError as e:
        return [str(e)]
    needed = [spec["opentofu"]["file"], spec["provider_proxmox"]["file"], "debs/Packages"]
    needed += [c["file"] for c in spec["ansible_collections"]]
    return [
        f"{name} is missing from /var/lib/snap; rebuild the USB stick"
        for name in needed
        if not ctx.host.exists(f"{ctx.payload}/{name}")
    ]


# --- OpenTofu ----------------------------------------------------------------


def _provider_dir(spec: dict) -> str:
    p = spec["provider_proxmox"]
    return f"{PROVIDER_MIRROR}/{p['source']}/{p['version']}/linux_amd64"


def tofu_done(ctx: Context) -> bool:
    spec = tools(ctx)
    if not ctx.host.ok(TOFU, "version"):
        return False
    version = ctx.host.run(TOFU, "version")
    return (
        f"v{spec['opentofu']['version']}" in version
        and bool(ctx.host.glob(f"{_provider_dir(spec)}/terraform-provider-proxmox*"))
        and ctx.host.exists(TOFU_CONFIG)
    )


def install_tofu(ctx: Context) -> None:
    """The tofu binary, plus the provider as a local mirror, so `tofu init` works offline."""
    spec = tools(ctx)
    ctx.host.unzip(f"{ctx.payload}/{spec['opentofu']['file']}", "/usr/local/bin", members=["tofu"])
    ctx.host.unzip(f"{ctx.payload}/{spec['provider_proxmox']['file']}", _provider_dir(spec))
    source = spec["provider_proxmox"]["source"]
    ctx.host.write(
        TOFU_CONFIG,
        "# Written by SNAP (CRACKLE): use the provider shipped on the stick, so OpenTofu works offline.\n"
        "provider_installation {\n"
        "  filesystem_mirror {\n"
        f'    path    = "{PROVIDER_MIRROR}"\n'
        f'    include = ["{source}"]\n'
        "  }\n"
        "  direct {\n"
        f'    exclude = ["{source}"]\n'
        "  }\n"
        "}\n",
    )


# --- Ansible -----------------------------------------------------------------


def _apt_options(ctx: Context) -> list[str]:
    """apt pointed only at the packages on the stick, without touching the host's own sources."""
    repo = f"{ctx.payload}/debs"
    return [
        "-o", f"Dir::Etc::SourceList={repo}/snap-local.list",
        "-o", f"Dir::Etc::SourceParts={repo}/sources.d",
        "-o", f"Dir::State::Lists={repo}/lists",
        "-o", "APT::Get::List-Cleanup=0",
    ]  # fmt: skip


def ansible_done(ctx: Context) -> bool:
    for package in tools(ctx)["debian_packages"]["install"]:
        try:
            status = ctx.host.run("dpkg-query", "-W", "-f=${Status}", package)
        except CommandError:
            return False
        if status != "install ok installed":
            return False
    return True


def install_ansible(ctx: Context) -> None:
    repo = f"{ctx.payload}/debs"
    ctx.host.write(f"{repo}/snap-local.list", f"deb [trusted=yes] file:{repo} ./\n")
    ctx.host.mkdir(f"{repo}/sources.d")
    ctx.host.mkdir(f"{repo}/lists/partial")
    packages = tools(ctx)["debian_packages"]["install"]
    env = ["env", "DEBIAN_FRONTEND=noninteractive"]
    ctx.host.run(*env, "apt-get", *_apt_options(ctx), "update", "-q")
    ctx.host.run(*env, "apt-get", *_apt_options(ctx), "install", "-y", "-q", "--no-install-recommends", *packages)


def _installed_collections(ctx: Context) -> dict[str, str]:
    if not ctx.host.ok("ansible-galaxy", "collection", "list", "-p", COLLECTIONS, "--format", "json"):
        return {}
    listing = json.loads(ctx.host.run("ansible-galaxy", "collection", "list", "-p", COLLECTIONS, "--format", "json"))
    found = {}
    for collections in listing.values():
        found.update({name: info.get("version", "") for name, info in collections.items()})
    return found


def collections_done(ctx: Context) -> bool:
    installed = _installed_collections(ctx)
    return all(installed.get(c["name"]) == c["version"] for c in tools(ctx)["ansible_collections"])


def install_collections(ctx: Context) -> None:
    archives = [f"{ctx.payload}/{c['file']}" for c in tools(ctx)["ansible_collections"]]
    ctx.host.run("ansible-galaxy", "collection", "install", "--offline", "--force", "-p", COLLECTIONS, *archives)


# --- API token for OpenTofu --------------------------------------------------


def _secret(ctx: Context) -> str | None:
    text = ctx.host.read(API_SECRET)
    return text.strip() if text else None


def token_done(ctx: Context) -> bool:
    secret = _secret(ctx)
    if not secret or not re.fullmatch(rf"{re.escape(API_USER)}!{API_TOKEN}=[0-9a-f-]+", secret):
        return False
    return ctx.host.http_ok(API_URL, {"Authorization": f"PVEAPIToken={secret}"})


def make_token(ctx: Context) -> None:
    """A role with only what OpenTofu needs, a user holding it, and a token for that user.

    The secret is shown once by Proxmox, so it is kept root-only for later
    `tofu apply` runs; if it was lost, the token is replaced.
    """
    privileges = ",".join(API_PRIVILEGES)
    roles = json.loads(ctx.host.run("pveum", "role", "list", "--output-format", "json"))
    verb = "modify" if any(r.get("roleid") == API_ROLE for r in roles) else "add"
    ctx.host.run("pveum", "role", verb, API_ROLE, "--privs", privileges)
    users = json.loads(ctx.host.run("pveum", "user", "list", "--output-format", "json"))
    if not any(u.get("userid") == API_USER for u in users):
        ctx.host.run("pveum", "user", "add", API_USER, "--comment", "SNAP automation (OpenTofu)")
    ctx.host.run("pveum", "acl", "modify", "/", "--users", API_USER, "--roles", API_ROLE)
    tokens = json.loads(ctx.host.run("pveum", "user", "token", "list", API_USER, "--output-format", "json"))
    if any(t.get("tokenid") == API_TOKEN for t in tokens):
        ctx.host.run("pveum", "user", "token", "remove", API_USER, API_TOKEN)
    created = json.loads(
        ctx.host.run(
            "pveum",
            "user",
            "token",
            "add",
            API_USER,
            API_TOKEN,
            "--privsep",
            "0",
            "--comment",
            "SNAP: OpenTofu",
            "--output-format",
            "json",
        )  # fmt: skip
    )
    ctx.host.mkdir("/var/lib/snap/secrets", mode=0o700)
    ctx.host.write(API_SECRET, f"{created['full-tokenid']}={created['value']}\n", mode=0o600)


STEPS = [
    Step("opentofu", "Install OpenTofu and the Proxmox provider", tofu_done, install_tofu),
    Step("ansible", "Install Ansible", ansible_done, install_ansible),
    Step("ansible-collections", "Install the Ansible collections", collections_done, install_collections),
    Step("api-token", "Create the OpenTofu API user and token", token_done, make_token),
]
