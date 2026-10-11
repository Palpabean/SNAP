"""The D2 spike: does OPNsense import SNAP's config.xml with nobody at the keyboard?

Boots the pinned OPNsense nano image in QEMU with the bootstrap config on an
attached ISO, answers the importer over the serial console exactly as POP
does on Proxmox, then checks that the API answers with the key from the config.

    python tests/opnsense/spike.py nano.img work/

The admin network is QEMU user networking with the API forwarded to
127.0.0.1:8443, standing in for the Proxmox host's admin VLAN. Writes the API
key and secret to work/api.env for the Ansible check that follows; with
--keep-running the VM stays up for it.
"""

from __future__ import annotations

import base64
import json
import secrets
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from snaplab.core import config
from snaplab.stages.pop import opnsense

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "snap.example.yaml"
API = "https://127.0.0.1:8443/api"


def api_get(path: str, key: str, secret: str):
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    request = urllib.request.Request(f"{API}/{path}", headers={"Authorization": f"Basic {token}"})
    with urllib.request.urlopen(request, context=ssl._create_unverified_context(), timeout=15) as r:
        return json.load(r)


def main(image: str, work: str) -> int:
    work_dir = Path(work)
    (work_dir / "iso" / "conf").mkdir(parents=True, exist_ok=True)
    cfg = config.load(EXAMPLE)
    key, secret = secrets.token_urlsafe(60), secrets.token_urlsafe(60)
    hashed = subprocess.run(
        ["openssl", "passwd", "-6", "-stdin"], input=secret, capture_output=True, text=True, check=True
    ).stdout.strip()
    (work_dir / "iso/conf/config.xml").write_text(opnsense.render_config(cfg, key, hashed))
    iso = work_dir / "config.iso"
    subprocess.run(["xorriso", "-as", "mkisofs", "-quiet", "-r", "-o", str(iso), str(work_dir / "iso")], check=True)

    sock = work_dir / "serial.sock"
    sock.unlink(missing_ok=True)
    admin = f"net={opnsense.ADMIN_CIDR},host={opnsense.ADMIN_HOST},dhcpstart={opnsense.ADMIN_CIDR[4]}"
    nics = [
        "-nic", "user,model=virtio-net-pci",  # vtnet0, WAN
        "-nic", f"user,model=virtio-net-pci,{admin},hostfwd=tcp:127.0.0.1:8443-{opnsense.ADMIN_ROUTER}:443",
    ]  # fmt: skip
    for _ in cfg["networks"]:
        nics += ["-nic", "user,model=virtio-net-pci,restrict=on"]
    qemu = subprocess.Popen(
        ["qemu-system-x86_64", "-enable-kvm", "-machine", "q35", "-cpu", "host", "-smp", "2", "-m", "2048",
         "-display", "none", "-serial", f"unix:{sock},server=on,wait=off",
         "-drive", f"file={image},format=raw,if=virtio,snapshot=on",
         "-drive", f"file={iso},media=cdrom,if=ide", *nics]  # fmt: skip
    )
    log = (work_dir / "opnsense-serial.log").open("w")

    def echo(text: str) -> None:
        log.write(text)
        log.flush()

    try:
        console = opnsense.Serial(str(sock))
        start = time.monotonic()
        opnsense.run_importer(console, log=echo)
        print(f"importer done after {time.monotonic() - start:.0f}s", flush=True)
        deadline = time.monotonic() + 900
        while True:
            try:
                status = api_get("core/firmware/status", key, secret)
                break
            except OSError as e:
                if time.monotonic() > deadline:
                    raise SystemExit(f"the API did not answer: {e}") from e
                # Drain the console so the guest never blocks on output.
                try:
                    console.wait_for("\0", timeout=10, log=echo)
                except TimeoutError:
                    pass
        print(f"API answered after {time.monotonic() - start:.0f}s: {json.dumps(status)[:300]}", flush=True)
        (work_dir / "api.env").write_text(f"OPNSENSE_KEY={key}\nOPNSENSE_SECRET={secret}\n")
        console.close()
        return 0
    finally:
        log.close()
        if len(sys.argv) < 4 or sys.argv[3] != "--keep-running":
            qemu.terminate()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
