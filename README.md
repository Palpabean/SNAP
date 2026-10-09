# snaplab: SNAP (Swift Network Automation Program)

# Design 0001: SNAP Architecture and Process

- **Status:** Draft, proof of concept
- **Date:** 2026-10-09 (revised the same day: open questions settled, see section 13)
- **Scope:** USB delivery only. Other delivery methods are future work.

---

## 1. Summary

SNAP (Swift Network Automation Program) turns a bare computer into a self-contained virtual lab. A user boots a USB stick, answers a few questions in a terminal wizard, and walks away. The computer is installed with Proxmox VE, configured, given a virtual router and firewall (OPNsense), and populated with Ubuntu VMs on isolated test networks. Everything needed is on the stick, so no internet connection is required.

The work is divided into four named stages:

| Stage | Name | Purpose |
| :--- | :--- | :--- |
| **1** | **SNAP** | Check the hardware, collect settings, install Proxmox unattended |
| **2** | **CRACKLE** | Configure the Proxmox host and prepare the VM images |
| **3** | **POP** | Build the router, firewall, virtual networks, and test VMs |
| **4** | **BANG** | Final checks, secret cleanup, and "your lab is ready" |

Remote access with Tailscale is **not** a stage. It is an optional operation, **ECHO**, that the user runs after BANG (see section 8).

---

## 2. Goals and non-goals

### Goals
- **One bootable USB** that works start to finish with no terminal skills.
- **Fully offline-capable installation.**
- **Safe by default:** no disk is erased without explicit, typed confirmation.
- **Resumable and idempotent:** a power cut or failure never leaves the user guessing.
- **Open source**, with delivery methods, interfaces, and stages cleanly separated so others can extend them.

### Non-goals for the proof of concept
- Windows images (deferred; the template system will allow them later).
- ZFS, mirrors, or multi-disk layouts (single-disk install only).
- Moving the host's own management connection behind the new router.
- PXE, IPMI, or other delivery methods.

---

## 3. Design principles

1. **Delivery, interface, and engine are separate.** A delivery method only boots the machine and supplies `snap.yaml` plus the payload. A frontend only writes `snap.yaml`. The engine does the work.
2. **One source of truth.** `snap.yaml` describes everything. The wizard writes it, the engine reads it, and a hand-written copy can run the whole pipeline with no interface (the headless mode, which is also how automated tests run).
3. **Idempotent, recorded steps.** Every step can be repeated safely, and its result is stored.
4. **Observe, don't assume.** The stick decides what to do by looking at the machine (is Proxmox already installed?), not by trusting flags it may have failed to write.
5. **Destructive actions are rare, explicit, and early.**
6. **Secrets are short-lived.** They are used, then removed.
7. **Run from the machine, not the media.** After the handoff, the engine, payload, state, and logs all live on the target's own disk, so the USB is not part of normal operation and failures can be diagnosed in place.

---

## 4. End-to-end process

```
USER'S COMPUTER                                    TARGET COMPUTER
---------------                                    ---------------
1. SNAP Builder
   download + verify
   write USB                 --USB-->        2. Boot "guided setup"
                                             3. Hardware check
                                             4. Wizard -> snap.yaml
                                             5. Typed disk confirmation
                                             -- SNAP --
                                             6. Proxmox unattended install, restart
                                             7. Handoff to the new system
                                                (USB no longer needed after this)
                                             -- CRACKLE --
                                             8. Host config, Ubuntu + OPNsense templates
                                             -- POP --
                                             9. OPNsense, networks, test VMs, tests
                                             -- BANG --
                                             10. Final checks, clean up secrets
                                             11. "Your lab is ready"
```

---

## 5. Components

### 5.1 SNAP Builder (runs on the user's own computer)
- Downloads the Proxmox, Ubuntu, and OPNsense images from their official sources.
- Verifies each against a pinned SHA-256 in `checksums.lock`.
- Prepares the Proxmox auto-install media with Proxmox's own `proxmox-auto-install-assistant prepare-iso --fetch-from partition --on-first-boot snap-firstboot.sh`. The assistant version is pinned to match the ISO version, because mismatches are a known source of answer-file errors.
- Prepares the bundle (engine, stages, offline packages, the OpenTofu binary and the `bpg/proxmox` provider mirrored for offline use).
- Writes the USB. Initially requires Linux (or WSL/container) because it assembles a live image.
- The project never hosts or redistributes the Proxmox, Ubuntu, or OPNsense images.

### 5.2 The USB
- A bootloader menu with: Boot through USB: guided setup (default), Proxmox auto-install, advanced command line, memory test, and boot from internal disk.
- A small FAT partition labeled `PROXMOX-AIS`, where the wizard writes the rendered `answer.toml`. The Proxmox installer looks for this label when the ISO is prepared with `--fetch-from partition`; the label is fixed by the installer, not by us.
- A data partition labeled `SNAPDATA` for `snap.yaml`, the payload, and `checksums.lock`.
- The prepared Proxmox ISO, booted from our own GRUB menu. How it is booted is the M1 spike (see decision D1 in section 13).

### 5.3 The live environment and TUI
- A small Debian-based environment that boots straight into a terminal wizard written in Python with the Textual library. It works over a VM console, serial console, or remote console, needs no graphics stack, and can be driven by scripted keystrokes in tests.
- **Screens:** boot menu, hardware check, start (guided, load a config file, advanced), Host, Network, Machines, Firewall, Review, typed disk confirmation. After the restart, the same app shows Progress, Finished, and the recovery screens on the host's console.
- **Behavior:** safe defaults everywhere; immediate validation; answers saved to `snap.yaml` as the user goes; Esc to back out; F1 for help on any field.
- **Disk safety screen:** the USB stick is excluded from the disk list, there is no default selection when several disks exist, and the user must type the last four characters of the disk's serial number.

### 5.4 The engine
- A Python library and runner that executes stages. It writes a state file and an event log; interfaces read them. The same engine runs in the live environment, on the host after restart, and headless in tests.
- Every stage has the same five parts:
  - `preflight`: can this stage run?
  - `plan`: what will change (this gives a dry-run mode)
  - `apply`: do the work, as named, numbered, idempotent steps
  - `verify`: confirm it worked
  - `rollback`: undo, where possible
- Failures are categorized so each maps to a recovery screen: retry, save a report, or start over.

---

## 6. The configuration file (`snap.yaml`)

Top-level sections (schema to be defined and versioned):
- `host`: hostname, domain, time zone, keyboard, management address mode, administrator password hash, alert email, storage layout, target disk (by serial or model)
- `networks`: a list; each has a name, CIDR, router address, DNS servers, DHCP settings, optional VLAN, upstream connection, and a policy (isolated, internet-only, open) plus custom rules
- `machines`: count, name prefix, image, CPU, memory, disk, default username, password hash or SSH key, which network, create-now or templates-only
- `options`: offline mode, logging level

Passwords are stored only as hashes. Real configs containing any secret are never committed; only sanitized examples are.

---

## 7. Stages in detail

### 7.1 SNAP (stage 1, live environment)
1. Verify the bundle checksums and hardware (UEFI/BIOS, memory, CPU virtualization, disks, network ports and link).
2. Validate `snap.yaml` against the schema.
3. Show the typed disk confirmation.
4. Render the Proxmox answer file (`answer.toml`) from `snap.yaml`. The target disk is selected by serial with a `disk-setup` filter, and the root password is written only as a hash. Validate it with `proxmox-auto-install-assistant validate-answer`.
5. Write it to the `PROXMOX-AIS` partition, set the next boot to the Proxmox auto-install, and reboot.
6. The installer runs unattended and restarts.
7. Handoff (section 7.5).

### 7.2 CRACKLE (stage 2, on the host)
1. Wait for the Proxmox services and API to be ready.
2. Configure package repositories (offline mirror from the bundle, or standard repositories if online).
3. Create a least-privilege role and API token for automation.
4. Install the automation tools from the offline bundle.
5. Create the network bridges: an uplink bridge and an internal VLAN-aware bridge. The management connection is left alone.
6. Ensure storage is ready for images and VM disks.
7. Import the Ubuntu cloud image and convert it to a template.
8. Import the OPNsense image and convert it to a template.
9. Verify: the API responds and both templates exist.

### 7.3 POP (stage 3, on the host)
1. Render the network model from `snap.yaml`: subnets, VLANs, DHCP, DNS, firewall rules.
2. Render a bootstrap `config.xml` that assigns the interfaces (WAN, LAN, one per lab network), sets the management address and an API key, and enables the API. Interface assignment goes here rather than through the API, because the API does not fully cover it.
3. Pack `config.xml` into a small ISO under `conf/config.xml` and attach it to the cloned OPNsense VM. The OPNsense importer reads ISO9660 media (since 22.1.7), so no console typing is needed.
4. Start OPNsense, wait for the API, then apply DHCP, DNS, aliases and firewall rules through it (the Ansible `oxlorg.opnsense` collection, formerly `ansibleguy.opnsense`). Pin the OPNsense version to the one the collection supports, because the collection tracks the newest OPNsense API.
5. Create the Ubuntu VMs with OpenTofu and the `bpg/proxmox` provider (or leave them as templates, per setting) with the chosen credentials through cloud-init.
6. Verify from a test VM: it receives an address, reaches the router, resolves DNS, and blocked paths are actually blocked.

The host's own management connection stays on the existing network. Moving it behind the router is an optional later feature that must use a confirm-or-revert timer; the "network change undone" screen exists for that case.

### 7.4 BANG (stage 4, on the host)
1. Re-run every stage's verify step as a final end-to-end check.
2. Write a summary: Proxmox web address, login, networks, VM names and addresses.
3. Scrub secrets from the payload and the state files, and keep only hashes and the summary.
4. Disable the automatic-resume service so the host boots normally from now on.
5. Mark the lab as ready and show the *Your lab is ready* screen.

### 7.5 The handoff between SNAP and CRACKLE
After the unattended install, the machine restarts and the USB, still first in the boot order, loads again. Nothing has run in between that could have recorded the install as finished, so the SNAP environment decides by **observing the machine**:
- If the target disk already holds a Proxmox installation, the default is to continue: inject a small bootstrap service and the payload into the new system, then boot the internal disk. Reinstalling requires an explicit choice with the disk confirmation again.
- If not, it starts the wizard.

This avoids the reinstall loop. After the handoff, **everything runs from the target computer's own disk**: the bootstrap service starts the engine at CRACKLE and runs through POP and BANG, resuming from the state file after any restart.

The USB is needed only until the handoff is verified:
- The payload copy is checked against the pinned checksums before the console says *"Handoff complete: you can remove the USB now."*
- If the copy fails, the USB still holds the original payload, so the handoff can be retried without rebuilding anything.
- If the user leaves the stick in, nothing breaks: on every boot the SNAP environment sees the existing installation and continues to the internal disk.

**Mechanism (decided, D3):** the handoff uses the Proxmox installer's own first-boot hook (`[first-boot]` with `source = "from-iso"`), which runs as the `proxmox-first-boot` one-shot service. The script `snap-firstboot.sh` is generic, so it is baked into the ISO at build time. It mounts `SNAPDATA` by label, copies and verifies the payload into `/var/lib/snap/`, installs the `snap-resume` service, and starts CRACKLE. Use `ordering = "fully-up"` (the default); `before-network` has reported failures. The observation logic above stays as the safety net: if the hook did not complete, the SNAP environment sees an installed but unhanded-off system on the next USB boot and injects the payload itself.

---

## 8. ECHO: remote access (after BANG, optional)

ECHO (`snap echo`) is remote access with Tailscale. It is a separate command the user runs when they choose. It requires an existing Tailnet and an internet connection, the one operation that cannot be offline. It would install the Tailscale plugin on OPNsense, join the Tailnet using an auth key or OAuth client supplied by the user, advertise the lab subnets, optionally act as an exit node, and verify the result. The user still approves the routes in their Tailscale admin console.

---

## 9. State, logging, and recovery

- State lives in a file on the target (for example under `/var/lib/snap/`), recording each stage and step as `pending`, `running`, `done`, or `failed`. Logs are written alongside it.
- After any restart, the engine resumes at the first unfinished step.
- Recovery screens: stage failed (try again, save a report, start over), network change undone (automatic revert), and hardware check (fix before continuing).
- Saved reports remove passwords and keys.
- Because state, logs, and the engine live on the host, problems can be diagnosed from the host console or over SSH, with no USB required.
- Planned host commands:
  - `snap status`: stage and step results
  - `snap logs`: inspect log
  - `snap resume`: continue from the first unfinished step
  - `snap report`: save a sanitized troubleshooting file
- The USB's advanced entry can mount an installed system to collect logs or re-inject the payload when a host is too broken to boot normally.

---

## 10. Security and secrets

- Passwords are hashed before they are written anywhere. The wizard never stores plaintext.
- Any Tailscale credential is used once. A single-use, expiring key is recommended.
- BANG removes secret material from the payload and state files.
- Checksums are pinned and verified before any artifact is used.
- Destructive operations always require the typed confirmation.

---

## 11. Repository layout and what stays out

```
core/        schema, engine, state machine
stages/      snap/ crackle/ pop/ bang/
frontends/   tui/ (first) headless/
delivery/    usb/ (first)
pop/tofu/    OpenTofu configuration for the lab VMs
tests/       docs/ examples/
checksums.lock
```

**Never committed:** ISOs and disk images, built USB images, the download cache, and any real `snap.yaml`. `.gitignore` covers these. The SNAP live image (our own code on Debian) may be published as a release artifact; third-party images never are.

---

## 12. Testing

1. **Every commit, no VMs:** schema validation, rendering of the answer file and OPNsense configuration, `ansible-lint`, `tofu validate`, `shellcheck`.
2. **Every commit:** scripted-keystroke tests of the TUI through the whole wizard, checking the resulting `snap.yaml`.
3. **On real hardware:** the full pipeline on a dedicated computer before milestones. Nested virtualization is deliberately avoided to prevent drift.
4. **Hardware compatibility list:** maintained from contributor reports.

---

## 13. Decisions (settled 2026-10-09)

| # | Question | Decision |
| :--- | :--- | :--- |
| D1 | **USB boot chain** | Still the largest risk, but narrowed. The answer file does not need to be inside the ISO: with `--fetch-from partition`, the ISO prepared by Proxmox's own assistant reads `answer.toml` from a partition labeled `PROXMOX-AIS`, so the wizard can write it at run time. The M1 spike tests, in order: **(a)** our GRUB menu loopback-boots the prepared ISO with no extra kernel arguments (Ventoy-style injected arguments are known to leak into the installed system, so we do not inject any); **(b)** the ISO contents extracted to their own partition. **Fallback:** run the wizard in the Builder and write the stick as the prepared ISO plus the `PROXMOX-AIS` and `SNAPDATA` partitions, with the hardware check moved to the first-boot script. Test on both UEFI and legacy BIOS. |
| D2 | **OPNsense automation** | Bootstrap `config.xml` delivered on an attached ISO through the OPNsense importer; it holds interface assignment and API access. Everything else goes through the API with `oxlorg.opnsense`. Still needs an early spike in M3 to confirm the importer runs unattended on first boot. |
| D3 | **Version-specific behavior** | Pin Proxmox VE 9.x (the latest point release at M1) together with the matching `proxmox-auto-install-assistant`. Use the installer's first-boot hook for the handoff (section 7.5). Pin OPNsense to the version `oxlorg.opnsense` supports. All three versions live in `checksums.lock`, and CI validates the answer file with the pinned assistant. |
| D4 | **Terraform licensing** | **OpenTofu only.** It is MPL-2.0, so it can be redistributed on the USB, and it uses the same `bpg/proxmox` provider. Terraform is not shipped or tested. |
| D5 | **Repository name** | **`snaplab`.** The project and stage names stay SNAP, CRACKLE, POP and BANG; the command stays `snap`. |
| D6 | **Remote-access name** | **ECHO** (`snap echo`). |

### Remaining risks
- The D1 spike may fail on some firmware; the hardware compatibility list records the results.
- First-boot hook reports from the community are mixed (for example a missing `/firstboot` folder); the observation safety net covers this.
- The OPNsense collection follows the newest OPNsense API, so OPNsense upgrades need a matching collection upgrade.

---

## 14. Milestones

- **M0:** repository, license, `snap.yaml` schema, test layers 1 and 2.
- **M1:** the USB boot-chain spike (D1), then SNAP end to end: wizard, answer file, unattended install, handoff.
- **M2:** CRACKLE with the Ubuntu template.
- **M3:** the OPNsense importer spike (D2), then POP with OPNsense, networks, test VMs, and policy tests.
- **M4:** BANG, recovery screens, the Builder, and hardware testing.
- **Later:** remote access, more delivery methods, Windows images, ZFS and mirrors.
