# SNAP

**SNAP (Swift Network Automation Program) turns a spare computer into a ready-to-use virtual lab from a single USB stick.**

Boot the spare computer from the stick, confirm a few settings, and about an hour later it is running:

- **Proxmox VE**, the hypervisor, managed from your browser.
- **An OPNsense router and firewall** that keeps the lab separate from your home network.
- **Two lab networks**: `lab` (internet access) and `services` (fully isolated).
- **Two Alpine Linux virtual machines** on the `lab` network, ready to log in to.

Everything the lab needs is on the stick, so the build works offline.

## Permanent or volatile?

SNAP comes as two sticks. Both build the same lab; they differ in where it lives.

| | Permanent lab | Volatile lab |
| :--- | :--- | :--- |
| **Where it runs** | Installed on the computer's disk | From the USB stick and the computer's memory |
| **The computer's disk** | **Erased** | Untouched |
| **After turning it off** | The lab is still there | The lab is gone; the next start builds a fresh one |
| **The USB stick** | Can be removed once it is set up | Must stay in while the lab runs |
| **Memory (RAM)** | 16 GB recommended, 8 GB minimum | 16 GB |
| **Setup screens** | Yes: change the name, networks, machines and firewall | No: the built-in lab, with no questions |
| **Download** | `snap.img.xz` | `snap-volatile.img.xz` |

Pick **permanent** for a lab you keep and come back to. Pick **volatile** to try SNAP, for a class or a
demo, or on a computer you cannot erase.

> **A permanent lab erases the spare computer's disk.** Use a machine with nothing on it you want to keep.

## Requirements

| | |
| :--- | :--- |
| **Spare computer** | 64-bit, virtualization (Intel VT-x / AMD-V) enabled, 16 GB RAM (8 GB minimum for a permanent lab), wired network connection; a permanent lab also needs a 128 GB+ disk |
| **USB stick** | 8 GB or larger (it is erased) |
| **Your own computer** | Windows, macOS or Linux, with about 4 GB free and [balenaEtcher](https://etcher.balena.io/) |

## Install

### 1. Make the stick

1. From the [latest release](https://github.com/Palpabean/SNAP/releases/latest), download **`snap.img.xz`**
   (permanent, about 2 GB) or **`snap-volatile.img.xz`** (volatile, about 1.6 GB).
2. Open balenaEtcher, choose **Flash from file**, select the file (no need to unzip it), pick the USB stick,
   and click **Flash**.

The stick contains no password. Each lab computer generates its own and shows it on screen.

### 2. Install the lab (permanent stick)

1. Plug the stick and a network cable into the spare computer and power it on.
2. Open the boot menu (usually **F12**, **F11**, **F8** or **Esc**) and choose the USB stick.
3. The SNAP menu appears and starts the **guided setup** after 10 seconds.
4. Step through the setup screens. The defaults are ready to use, so **Next** on each screen is enough:
   - **Host**: name, time zone, keyboard and the **password**. Write the password down.
   - **Network**, **Machines**, **Firewall**: the lab's layout, editable if you want.
   - **Review**: choose the disk, confirm it may be erased, and click **Start installation**.

   If the first screen reports a problem (for example, virtualization is disabled), fix it in the
   computer's firmware settings and click **Check again**.
5. Wait about an hour. Proxmox installs, the computer restarts, and the lab builds itself. The
   screen shows progress throughout. Leave the stick in until it says **Your lab is ready**.

Prefer no questions at all? Choose **Install with SNAP's settings, no questions asked** in the
menu. SNAP shows the disk it will use and erases it after 15 seconds (with more than one disk it
asks which one, and you type `ERASE` to confirm).

**Volatile stick:** steps 1 and 2 are the same; then the SNAP menu starts the volatile lab after
10 seconds. There are no setup screens and nothing is installed: the SNAP start-up screen appears,
and about 10 to 20 minutes later the screen says **Your lab is ready**. Keep the stick in the whole
time, and remember that turning the computer off deletes the lab.

[Pictures of every screen, in order](docs/screens/README.md)

### 3. Open the lab

1. The lab computer's screen shows the web address (for example `https://192.168.1.50:8006`),
   the user name `root`, and the password.
2. Open that address in a browser on your own computer. The certificate warning is expected for a
   new lab: choose **Advanced**, then **Continue**.
3. Log in as `root`.

You will find the router `snap-router` and the machines `alpine-01` and `alpine-02`. To use a
machine, select it and open **Console**; log in as `labuser` with the same password. A summary of
the lab is saved on the lab computer as `/root/snap-lab.txt`.

## Troubleshooting

| Problem | What to do |
| :--- | :--- |
| The computer does not boot from the stick | In the firmware settings (often **F2** or **Del** at power-on), enable USB boot. |
| "Virtualization is turned off" | Enable Intel VT-x or AMD-V (sometimes called SVM) in the firmware settings. |
| "No internal disk found" | The disk is not detected. Check it in the firmware settings. |
| The install fails during setup | The screen explains why. Click **Try again**, or **Turn off computer** to fix the cause first. |
| The lab screen says a step **needs attention** | Press **R** to retry; SNAP resumes where it stopped. Press **L** to view the log. Restarting the computer also retries. |
| Lost the password | It is on the lab computer's screen: the "ready" screen shows it, or press **P** while it is building. |

## For developers

<details>
<summary>Building the stick, options and tests</summary>

### Build the stick yourself

Needs Docker and about 10 GB free. The first build downloads the Proxmox installer (about 1.7 GB)
and verifies it against `checksums.lock`.

```sh
docker build --platform linux/amd64 -t snaplab-builder -f snaplab/delivery/usb/Containerfile .
docker run --rm --platform linux/amd64 -v "$PWD:/work" snaplab-builder build -o snap.img
```

The build prints a password (also saved as `snap.img.password.txt`), used by the "no questions
asked" install. Write `snap.img` to a stick with balenaEtcher.

### Options

- **Change the built-in lab:** pass a `snap.yaml` with only the settings to change:
  `build my-changes.yaml -o snap.img`. All settings: [examples/snap.example.yaml](examples/snap.example.yaml),
  schema: [snaplab/core/schema/snap.schema.json](snaplab/core/schema/snap.schema.json).
- **Release image:** `--release` builds the published image, with no password inside; each lab
  computer generates its own at first boot. A `v*` tag (pushed, or created with **Draft a new
  release** on GitHub) builds it, compresses it to `snap.img.xz` and attaches it to the release
  (`.github/workflows/release.yml`).
- **Volatile stick:** `build --volatile -o snap-volatile.img` builds a ready-made Proxmox system from the
  ISO's own packages and puts it on the stick; it needs root, so run the container with `--privileged`.
- **Unattended:** `--unattended text` makes "no questions asked" the menu default;
  `--unattended wizard` runs the setup screens with their defaults (used by the automated tests).
- **Serial console:** `--serial-console` for headless machines.
- **On the lab computer:** `snap status` shows each step, `snap logs` the log, `snap resume`
  continues after a failure, and `snap screen` is the progress screen.

### How it works

The stick runs the Proxmox installer with an answer file, then hands over to SNAP on the installed
system, which builds the lab in stages (host preparation, router and networks, machines, final
checks). Each step is idempotent and resumable. Details: [docs/design/0001-architecture.md](docs/design/0001-architecture.md).

### Tests

Python 3.11 or newer:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check . && ruff format --check .
pytest
```

The setup screen tests also need Playwright (`pip install playwright`) for the page, and `xvfb`
with `libgtk3-webkit2-perl` for the real window; they are skipped without them. CI also installs
the whole thing on a virtual machine, from the stick to the ready lab, in BIOS and UEFI mode.

Never commit a real `snap.yaml`, password files, ISOs or disk images. `.gitignore` covers them.

</details>

## License

[Apache License 2.0](LICENSE)
