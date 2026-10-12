"""The setup wizard's files, as they go on the stick (Design 0001, section 5.3).

The wizard (snaplab/delivery/usb/wizard/) is a web page shown by
snap-wizard.pl in the Proxmox installer's own graphical environment. The
builder fills the logo into the page and adds config.json: the stick's
settings without any password, which the wizard starts from.
"""

from __future__ import annotations

import copy
import json
from importlib import resources
from typing import Any

from snaplab.core import logo

PACKAGE = "snaplab.delivery.usb"
FILES = ["index.html", "wizard.css", "wizard.js", "snap-wizard.pl", "SnapWizard.pm", "wizard-setup.sh"]


def settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """What the wizard starts from: the stick's settings, never its password hashes."""
    out = copy.deepcopy(cfg)
    out["host"].pop("root_password_hash", None)
    out["host"].pop("target_disk", None)
    out["machines"].pop("password_hash", None)
    return out


def files(cfg: dict[str, Any]) -> dict[str, bytes]:
    """{file name: content} for the wizard directory."""
    folder = resources.files(PACKAGE).joinpath("wizard")
    out = {name: folder.joinpath(name).read_bytes() for name in FILES}
    out["index.html"] = out["index.html"].decode().replace("@LOGO@", logo.html()).encode()
    out["config.json"] = json.dumps(settings(cfg), indent=2).encode()
    return out
