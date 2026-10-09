# SNAP (Swift Network Automation Program)

# Design 0001: SNAP Architecture and Process

- **Status:** Draft, proof of concept
- **Date:** 2026-10-09
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

Remote access with Tailscale is **not** a stage. It is an optional operation the user runs after BANG (see section 8).

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
- Prepares the Proxmox auto-install media and the bundle (engine, stages, offline packages).
- Writes the USB. Initially requires Linux (or WSL/container) because it assembles a live image.
- The project never hosts or redistributes the Proxmox, Ubuntu, or OPNsense images.

### 5.2 The USB
- A bootloader menu with: Boot through USB: guided setup (default), Proxmox auto-install, advanced command line, memory test, and boot from internal disk.
- A data partition for `snap.yaml`, the installer answer file, and the payload.
- Open question: whether one stick can hold both the SNAP environment and the unmodified Proxmox installer reliably across firmware types (see section 11).

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
4. Render the Proxmox answer file from `snap.yaml`.
5. Place it where the installer will find it, set the next boot to the Proxmox auto-install, and reboot.
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
2. Clone the OPNsense VM, attach interfaces, inject a bootstrap configuration, and start it.
3. Wait for OPNsense, then apply interfaces, DHCP, and firewall rules through its API.
4. Create the Ubuntu VMs (or leave them as templates, per setting) with the chosen credentials.
5. Verify from a test VM: it receives an address, reaches the router, resolves DNS, and blocked paths are actually blocked.

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

Proxmox's own first-boot hook is a possible alternative if the pinned version supports it, but it still needs the payload from the stick.

---

## 8. Remote access (after BANG, optional)

Remote access with Tailscale is a separate command the user runs when they choose. It requires an existing Tailnet and an internet connection, the one operation that cannot be offline. It would install the Tailscale plugin on OPNsense, join the Tailnet using an auth key or OAuth client supplied by the user, advertise the lab subnets, optionally act as an exit node, and verify the result. The user still approves the routes in their Tailscale admin console. A name for this operation is undecided.

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
tests/       docs/ examples/
checksums.lock
```

**Never committed:** ISOs and disk images, built USB images, the download cache, and any real `snap.yaml`. `.gitignore` covers these. The SNAP live image (our own code on Debian) may be published as a release artifact; third-party images never are.

---

## 12. Testing

1. **Every commit, no VMs:** schema validation, rendering of the answer file and OPNsense configuration, `ansible-lint`, `terraform validate`, `shellcheck`.
2. **Every commit:** scripted-keystroke tests of the TUI through the whole wizard, checking the resulting `snap.yaml`.
3. **On real hardware:** the full pipeline on a dedicated computer before milestones. Nested virtualization is deliberately avoided to prevent drift.
4. **Hardware compatibility list:** maintained from contributor reports.

---

## 13. Open questions and risks

1. **USB boot chain:** One stick holding the SNAP environment and the unmodified Proxmox installer is the largest unknown. Fallback: run the wizard in the Builder on the user's computer, and make the stick the Proxmox auto-install only.
2. **OPNsense automation:** Bootstrap by injected configuration, then API configuration, needs an early spike.
3. **Version-specific behavior:** Answer-file options, first-boot hooks, and installer partition labeling should be verified against the pinned Proxmox release before relying on them.
4. **Terraform licensing:** Terraform moved to the BUSL license; OpenTofu is the open-source fork with compatible configuration and providers. Decide which the bundle ships, or support both.
5. **Repository name:** `snap` collides with Ubuntu's `snap`; a distinct repository name is preferable.
6. **Name for the remote-access operation.**

---

## 14. Milestones

- **M0:** repository, license, `snap.yaml` schema, test layers 1 and 2.
- **M1:** the USB boot-chain spike, then SNAP end to end: wizard, answer file, unattended install, handoff.
- **M2:** CRACKLE with the Ubuntu template.
- **M3:** POP with OPNsense, networks, test VMs, and policy tests.
- **M4:** BANG, recovery screens, the Builder, and hardware testing.
- **Later:** remote access, more delivery methods, Windows images, ZFS and mirrors.
