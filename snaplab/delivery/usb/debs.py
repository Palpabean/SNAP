"""Bundle Debian packages for offline install on the host (design decision D7).

Runs in the builder container, which is Debian 13 like Proxmox VE 9. apt
downloads the packages with their full dependency closure (as if nothing were
installed, so nothing the host might lack is skipped) and verifies them
against Debian's signatures. The directory gets a Packages index, so the host
can use it as a local apt repository: apt then installs only what is missing
and keeps the host's own, possibly newer, versions of everything else.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from snaplab.delivery.usb.image import ImageError, run


def download(packages: list[str], dest: Path) -> None:
    if os.geteuid() != 0:
        raise ImageError("bundling Debian packages needs apt as root; run the builder in its container")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "partial").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        empty_status = Path(tmp) / "status"
        empty_status.write_text("")
        run(["apt-get", "update", "-q"])
        run(
            [
                "apt-get",
                "install",
                "-y",
                "-q",
                "--download-only",
                "--no-install-recommends",
                "-o",
                f"Dir::State::status={empty_status}",
                "-o",
                f"Dir::Cache::Archives={dest}",
                *packages,
            ]  # fmt: skip
        )
    (dest / "partial").rmdir()
    (dest / "lock").unlink(missing_ok=True)


def write_index(repo: Path) -> dict[str, str]:
    """Write a Packages index for the .deb files in repo; return {package: version}."""
    stanzas, versions = [], {}
    for deb in sorted(repo.glob("*.deb")):
        control = subprocess.run(["dpkg-deb", "-f", str(deb)], check=True, capture_output=True, text=True).stdout
        fields = dict(line.split(": ", 1) for line in control.splitlines() if ": " in line and not line[0].isspace())
        versions[fields["Package"]] = fields["Version"]
        digest = hashlib.sha256(deb.read_bytes()).hexdigest()
        stanzas.append(
            control.rstrip("\n") + f"\nFilename: ./{deb.name}\nSize: {deb.stat().st_size}\nSHA256: {digest}\n"
        )
    (repo / "Packages").write_text("\n".join(stanzas))
    return versions
