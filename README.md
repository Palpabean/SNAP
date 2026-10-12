# SNAP

**SNAP (Swift Network Automation Program) turns a spare computer into a ready-made practice lab.**

You make a USB stick, start the spare computer from it, and walk away. About an hour
later the computer is a working lab:

- **Proxmox**: the free program that runs virtual computers, with a web page to manage them.
- **A router and firewall** (OPNsense) that keeps the lab separate from your home network.
- **Two lab networks**: one that can reach the internet, and one that is completely closed off.
- **Two Ubuntu Linux machines**, ready to log in to.

Everything comes from the stick. Nothing needs to be typed in, and the lab builds without an
internet connection.

> **The spare computer is wiped.** Use one with nothing on it you want to keep.

## How it works

1. **Make the stick** on your own computer (Windows, Mac or Linux): download the ready-made
   stick from the [latest release](https://github.com/Palpabean/SNAP/releases/latest) and write
   it to a USB stick with a free program. (You can also build it yourself; see the guide.)
2. **Start the spare computer from the stick.** The SNAP menu appears.
3. **Click through the setup screens.** Everything is already filled in, so you can just press
   **Next** on each one. Write down the password shown on the first screen (the lab computer's
   screen shows it again at the end).
4. **Wait.** The computer installs, restarts by itself and builds the lab. Its screen shows each
   step as it goes.
5. **Open your lab.** When the screen says **Your lab is ready**, type the web address it shows
   into the browser on your own computer, and log in.

**Full step-by-step guide: [docs/INSTALL.md](docs/INSTALL.md)**
**Pictures of every screen, in order: [docs/screens](docs/screens/README.md)**

## What you need

- Your own computer with about 4 GB of free space, and [balenaEtcher](https://etcher.balena.io/) to write the stick.
- A USB stick of 8 GB or more (it is erased).
- The spare computer: 16 GB of memory (8 GB at the very least), a disk of 128 GB or more,
  virtualization turned on in its setup screen (the setup screens check this for you),
  and a network cable to your router.

## The four stages

SNAP builds the lab in four stages, named after the sound of a spark. The screens show each one.

| Stage | What it does | Ready? |
| :--- | :--- | :--- |
| **SNAP** | The USB stick: checks the computer, asks its questions, installs Proxmox | Yes |
| **CRACKLE** | Prepares the new Proxmox computer and the tools that build the lab | Yes |
| **POP** | Builds the router, the networks and the Ubuntu machines, then checks they work | Yes |
| **BANG** | Checks the whole lab once more, writes a summary, removes spare copies of keys and passwords | Yes |

When BANG finishes, the lab computer's screen says **Your lab is ready**, and a summary of the
lab is saved on it as `/root/snap-lab.txt`. Every stage is tested automatically by installing
the whole thing on a virtual computer, from the stick to the ready lab.

## If something goes wrong

The lab computer's screen says what happened and what to try. Usually that means pressing
**R** to try again: SNAP continues from where it stopped and never starts over on its own.
More help is at the end of [docs/INSTALL.md](docs/INSTALL.md).

## For developers

<details>
<summary>Building, options and tests</summary>

Build the stick (the first build downloads the Proxmox installer, about 1.7 GB, and checks it
against `checksums.lock`):

```sh
docker build -t snaplab-builder -f snaplab/delivery/usb/Containerfile .
docker run --rm -v "$PWD:/work" snaplab-builder build -o snap.img
```

- **Change the built-in lab:** write a `snap.yaml` with only the settings to change and add it:
  `build my-changes.yaml -o snap.img`. All settings: [examples/snap.example.yaml](examples/snap.example.yaml),
  schema: [snaplab/core/schema/snap.schema.json](snaplab/core/schema/snap.schema.json).
- **The download image:** `--release` builds the stick to publish: no password in it; each lab
  computer picks its own at first start and shows it on its screen. Pushing a tag like `v1.0.0`
  builds it, compresses it (`snap.img.xz`) and publishes it as a GitHub release
  (`.github/workflows/release.yml`).
- **No questions at all:** `--unattended text` makes "no questions asked" the menu's default;
  `--unattended wizard` runs the setup screens with their defaults (both are for automated tests).
- **Serial console:** `--serial-console` for computers without a screen.
- **On the lab computer:** `snap status` shows each step, `snap logs` the log, `snap resume`
  continues after a failure, and `snap screen` is the setup screen itself.

How it is built: [docs/design/0001-architecture.md](docs/design/0001-architecture.md).

Tests need Python 3.11 or newer:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check . && ruff format --check .
pytest
```

The setup screens' tests also need Playwright (`pip install playwright`) for the page, and
`xvfb` with `libgtk3-webkit2-perl` for the real window; they are skipped without them.

Never commit a real `snap.yaml`, password files, ISOs or disk images. `.gitignore` covers them.

</details>

## License

[Apache License 2.0](LICENSE)
