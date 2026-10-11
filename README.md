# snaplab

SNAP (Swift Network Automation Program) turns a bare computer into a self-contained virtual lab. You boot a USB stick, answer a few questions, and walk away. The stick installs Proxmox VE, then builds an OPNsense router and firewall, isolated test networks and Ubuntu VMs, all without an internet connection.

| Stage | Name | Purpose |
| :--- | :--- | :--- |
| 1 | **SNAP** | Check the hardware, collect settings, install Proxmox unattended |
| 2 | **CRACKLE** | Configure the Proxmox host and prepare the VM images |
| 3 | **POP** | Build the router, firewall, virtual networks and test VMs |
| 4 | **BANG** | Final checks, secret cleanup, and "your lab is ready" |

**Status:** proof of concept, milestone M2. The USB stick installs Proxmox VE unattended from a hand-written `snap.yaml`, hands off to the installed system, and runs CRACKLE: package sources, the internal lab bridge, storage, an Ubuntu 24.04 VM template, and the automation tools (OpenTofu with the Proxmox provider, Ansible with its collections, and a least-privilege API token), all offline. POP (router, networks, test VMs), BANG, and the guided wizard come next. On the host, `snap status` shows progress and `snap logs` the engine log.

## Build a stick

You need Linux (or WSL) with podman or docker, and a USB stick of 4 GB or more.

1. Copy `examples/snap.example.yaml` to `my-snap.yaml` and fill it in. Find the target disk's serial with `lsblk -o NAME,SERIAL,MODEL,SIZE` from any Linux live system; **the installer erases the disk with that serial**.
2. Build the image (the first build downloads the 1.7 GB Proxmox ISO and checks it against `checksums.lock`):

   ```sh
   podman build -t snaplab-builder -f snaplab/delivery/usb/Containerfile .
   podman run --rm -v "$PWD:/work" snaplab-builder build my-snap.yaml -o snap.img
   ```

3. Write `snap.img` to the stick with `dd`, balenaEtcher, or Rufus (DD mode). Boot the target computer from it, with Secure Boot on or off.

The stick installs Proxmox, restarts, boots the new system, copies what it needs, and shows *Handoff complete: you can remove the USB now.* If it is left in, it boots the installed system instead of reinstalling.

- Design: [docs/design/0001-architecture.md](docs/design/0001-architecture.md)
- Example config: [examples/snap.example.yaml](examples/snap.example.yaml)
- Schema: [snaplab/core/schema/snap.schema.json](snaplab/core/schema/snap.schema.json)

## Development

Requires Python 3.11 or newer.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check . && ruff format --check .
pytest
snaplab validate examples/snap.example.yaml
```

Never commit a real `snap.yaml`, ISOs or disk images. `.gitignore` covers them.

## License

[Apache License 2.0](LICENSE)
