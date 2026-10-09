# snaplab

SNAP (Swift Network Automation Program) turns a bare computer into a self-contained virtual lab. You boot a USB stick, answer a few questions, and walk away. The stick installs Proxmox VE, then builds an OPNsense router and firewall, isolated test networks and Ubuntu VMs, all without an internet connection.

| Stage | Name | Purpose |
| :--- | :--- | :--- |
| 1 | **SNAP** | Check the hardware, collect settings, install Proxmox unattended |
| 2 | **CRACKLE** | Configure the Proxmox host and prepare the VM images |
| 3 | **POP** | Build the router, firewall, virtual networks and test VMs |
| 4 | **BANG** | Final checks, secret cleanup, and "your lab is ready" |

**Status:** proof of concept, milestone M0. Nothing installs anything yet. What exists is the `snap.yaml` schema and its validator.

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
