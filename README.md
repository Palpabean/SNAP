# snaplab

SNAP (Swift Network Automation Program) turns a bare computer into a self-contained virtual lab. You build a USB stick with one command, boot the computer from it, and walk away. The stick installs Proxmox VE, then builds an OPNsense router and firewall, isolated test networks and Ubuntu VMs, all without an internet connection.

| Stage | Name | Purpose |
| :--- | :--- | :--- |
| 1 | **SNAP** | Check the hardware, collect settings, install Proxmox unattended |
| 2 | **CRACKLE** | Configure the Proxmox host and prepare the VM images |
| 3 | **POP** | Build the router, firewall, virtual networks and test VMs |
| 4 | **BANG** | Final checks, secret cleanup, and "your lab is ready" |

**Status:** proof of concept, milestone M3. The USB stick installs Proxmox VE unattended (no config file needed: SNAP has a built-in lab, and asks on the computer only which disk to use when there is more than one), hands off to the installed system, and runs CRACKLE: package sources, the internal lab bridge, storage, an Ubuntu 24.04 VM template, and the automation tools (OpenTofu with the Proxmox provider, Ansible with its collections, and a least-privilege API token), all offline. Then POP builds the lab: an OPNsense router whose config is imported unattended over its serial console, DHCP and firewall rules per network through its API with Ansible, the Ubuntu lab machines with OpenTofu, and a check from inside the lab. Lab address ranges that clash with the home network move by themselves. BANG comes next. On the host, `snap status` shows progress and `snap logs` the engine log.

## Build a stick

You need Linux (or WSL) with podman or docker, and a USB stick of 4 GB or more.

1. Build the image (the first build downloads the 1.7 GB Proxmox ISO and checks it against `checksums.lock`):

   ```sh
   podman build -t snaplab-builder -f snaplab/delivery/usb/Containerfile .
   podman run --rm -v "$PWD:/work" snaplab-builder build -o snap.img
   ```

   That builds SNAP's built-in lab: two networks (`lab` with internet, `services` isolated) and two Ubuntu machines. The build ends by printing the lab's password, which it also saves in `snap.img.password.txt`. To change something, write a `snap.yaml` with just those settings (see [examples/snap.example.yaml](examples/snap.example.yaml) for all of them) and add it to the command: `build my-changes.yaml -o snap.img`.
2. Write `snap.img` to the stick with `dd`, balenaEtcher, or Rufus (DD mode). Boot the target computer from it, with Secure Boot on or off.
3. The stick shows the disk it will install on and **erases it** after a 15-second countdown (press Enter to choose another or cancel). If the computer has more than one internal disk, it lists them and asks which one, then asks you to type `ERASE`. USB drives are never offered.

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
