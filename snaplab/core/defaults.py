"""SNAP's built-in lab, so a stick can be built with no snap.yaml at all.

`snaplab build` with no file uses these settings as they are. A snap.yaml may
also give only the parts to change (say, the time zone); everything it leaves
out comes from here. Lists, such as the networks, are replaced as a whole.

Any password not given is generated: one password for Proxmox, the router and
the lab machines, shown at the end of the build and saved next to the image.
The install disk is asked for on the target computer when the stick boots.
"""

from __future__ import annotations

import copy
import secrets
import subprocess
from typing import Any

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "host": {
        "hostname": "snap",
        "domain": "lab.internal",
        "country": "us",
        "timezone": "UTC",
        "keyboard": "en-us",
        "management": {"mode": "dhcp"},
        "email": "admin@lab.internal",
        "storage": {"filesystem": "ext4"},
    },
    "networks": [
        {
            "name": "lab",
            "cidr": "10.10.10.0/24",
            "router": "10.10.10.1",
            "vlan": 10,
            "dhcp": {"enabled": True, "start": "10.10.10.100", "end": "10.10.10.199"},
            "policy": "internet-only",
            "rules": [
                {
                    "action": "allow",
                    "to": "services",
                    "protocol": "tcp",
                    "ports": [22, 443],
                    "description": "Lab machines may reach the service network over SSH and HTTPS",
                }
            ],
        },
        {
            "name": "services",
            "cidr": "10.10.20.0/24",
            "router": "10.10.20.1",
            "vlan": 20,
            "dhcp": {"enabled": True, "start": "10.10.20.100", "end": "10.10.20.199"},
            "policy": "isolated",
        },
    ],
    "machines": {
        "count": 2,
        "name_prefix": "alpine",
        "image": "alpine-3.22",
        "cpu": 2,
        "memory_mb": 2048,
        "disk_gb": 20,
        "network": "lab",
        "username": "labuser",
        "mode": "create",
    },
    "options": {"offline": True, "log_level": "info"},
}

# Easy to read and type: no 0/O, 1/l/I.
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def merge(base: Any, override: Any) -> Any:
    """`override` on top of `base`: mappings merge key by key, anything else replaces."""
    if isinstance(base, dict) and isinstance(override, dict):
        out = copy.deepcopy(base)
        for key, value in override.items():
            out[key] = merge(base[key], value) if key in base else copy.deepcopy(value)
        return out
    return copy.deepcopy(override)


def generate_password() -> str:
    return "-".join("".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(4))


def hash_password(password: str) -> str:
    """SHA-512 crypt ($6$), which Proxmox, OPNsense and Alpine all accept."""
    out = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"openssl could not hash the password: {out.stderr.strip()}")
    return out.stdout.strip()


def lock_passwords(cfg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """For a stick everyone downloads: whatever has no password gets one nobody knows (a random
    one, thrown away). The lab computer then picks its own at first start (firstboot.sh).
    Returns the config and whether anything was locked."""
    cfg = copy.deepcopy(cfg)
    locked = hash_password(secrets.token_urlsafe(32))
    missing_root = not cfg["host"].get("root_password_hash")
    missing_lab = not cfg["machines"].get("password_hash") and not cfg["machines"].get("ssh_keys")
    if missing_root:
        cfg["host"]["root_password_hash"] = locked
    if missing_lab:
        cfg["machines"]["password_hash"] = locked
    return cfg, missing_root or missing_lab


def fill_passwords(cfg: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Give a generated password to whatever has none; return the config and that password (or None)."""
    cfg = copy.deepcopy(cfg)
    missing_root = not cfg["host"].get("root_password_hash")
    missing_lab = not cfg["machines"].get("password_hash") and not cfg["machines"].get("ssh_keys")
    if not (missing_root or missing_lab):
        return cfg, None
    password = generate_password()
    hashed = hash_password(password)
    if missing_root:
        cfg["host"]["root_password_hash"] = hashed
    if missing_lab:
        cfg["machines"]["password_hash"] = hashed
    return cfg, password
