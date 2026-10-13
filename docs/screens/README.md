# What you will see

These are SNAP's real screens, in the order you meet them. They are made from SNAP itself
(see the end of this page), so they match what the USB stick shows.

## On the USB stick

### 1. The SNAP menu

The first thing you see after starting the computer from the stick. After 10 seconds it
starts the guided setup by itself; press Enter to start it straight away.
**Install with SNAP's settings, no questions asked** skips the setup screens below.

![The SNAP boot menu](01-boot-menu.png)

### 2. Hardware check (only if something needs fixing)

Before anything else, SNAP checks the computer. You only see this screen if something has
to be fixed first, like virtualization being turned off in the computer's setup screen.
Fix it, then click **Check again**.

![Hardware check](02-hardware-check.png)

### 3. Your Proxmox computer

The computer's name, time zone and keyboard, and your **password**. Write the password
down: you need it to log in later. Everything is filled in already, so you can just press
**Next** on this and every following screen.

![Host settings](03-host.png)

### 4. Networks

The lab's networks. You can keep them as they are, change them, or add more.

![Networks](04-network.png)

![Adding a network](05-network-add.png)

### 5. Machines

How many Linux machines (Alpine Linux) to make, and how big each one is.

![Machines](06-machines.png)

### 6. Firewall

What each network is allowed to reach: only the internet, nothing at all (isolated), or
everything. You can also add your own rules.

![Firewall](07-firewall.png)

![Adding a firewall rule](08-firewall-rules.png)

### 7. Review

Pick the disk to install on, tick the box to confirm it will be erased, and click
**Start installation**.

![Review](09-review.png)

### 8. Installing

A progress bar while Proxmox installs. **Show technical details** shows what the
installer is doing.

![Installing](10-installing.png)

![Installed](11-installed.png)

If the install fails, the screen says why. Click **Try again**, or **Turn off computer**
to fix the problem first.

![Install failed](12-install-failed.png)

## On the lab computer, after the restart

The computer restarts by itself and keeps building the lab. Its own screen shows how far
along it is.

### 9. Setting up

A checklist of the four stages, the step SNAP is on, and roughly how long is left. Press
**P** to see the password again, or **L** to see the log.

![Setting up](13-lab-setting-up.png)

### 10. Your lab is ready

The web address to open in your browser, the user name and password, and what was built.

![Lab ready](14-lab-ready.png)

### If something goes wrong

SNAP stops safely and says what happened. Press **R** to try again: it continues from the
step that stopped.

![Needs attention](15-lab-needs-attention.png)

---

<details>
<summary>For developers: making these pictures again</summary>

```sh
python docs/screens/make_screens.py          # all of them
python docs/screens/make_screens.py --host   # only the lab computer's screens
```

The boot menu comes from a test stick booted in QEMU, the setup screens from the real page in
Chromium, and the lab computer's screens from `snap screen`'s own output. It needs Pillow, plus
what those tests need (Playwright with Chromium; QEMU, OVMF and the disk-image tools).

</details>
