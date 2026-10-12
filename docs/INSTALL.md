# Installing SNAP: step by step

SNAP turns a spare computer into a ready-made virtual lab: Proxmox, a router and
firewall, two test networks and two Ubuntu machines. You make a USB stick on
your own computer, plug it into the spare computer, and let it work.

**The spare computer is wiped.** Use one with nothing on it you want to keep.

## What you need

- **Your own computer** (Windows, Mac or Linux) with about 4 GB of free space.
- **A USB stick of 8 GB or more.** Anything on it is erased.
- **The spare computer** (the one that becomes the lab):
  - 16 GB of memory (8 GB is the bare minimum),
  - a disk of 128 GB or more,
  - plugged into your router with a network cable (for internet inside the lab).

## Part 1: make the stick (on your own computer, about 15 minutes)

1. **Install [balenaEtcher](https://etcher.balena.io/)**, a free program that writes the stick.

2. **Download the SNAP stick:** open the
   [latest SNAP release](https://github.com/Palpabean/SNAP/releases/latest) and click
   **snap.img.xz** (a few GB). There is no need to unzip it.

3. **Write the stick:** open balenaEtcher, click **Flash from file**, choose `snap.img.xz`,
   choose your USB stick, and click **Flash**.

That's it. The stick holds no password: the lab computer picks its own and shows it on its
screen (Part 2).

<details>
<summary>Prefer to build the stick yourself? (to change the built-in lab, for example)</summary>

You also need [Docker Desktop](https://www.docker.com/products/docker-desktop/) (open it once and
leave it running) and about 10 GB of free space.

1. **Download SNAP:** on the [SNAP GitHub page](https://github.com/Palpabean/SNAP), click the green
   **Code** button, then **Download ZIP**. Unzip it somewhere easy, like your Desktop.
2. **Open a terminal in the SNAP folder:**
   - Windows: open the unzipped folder, click the address bar, type `powershell` and press Enter.
   - Mac: right-click the folder and choose **New Terminal at Folder**.
   - Linux: right-click inside the folder and choose **Open in Terminal**.
3. **Build the stick.** Copy these two lines into the terminal, one at a time, pressing Enter after
   each. The first time takes a while because it downloads about 3 GB.

   ```sh
   docker build --platform linux/amd64 -t snaplab-builder -f snaplab/delivery/usb/Containerfile .
   docker run --rm --platform linux/amd64 -v "${PWD}:/work" snaplab-builder build -o snap.img
   ```

4. **Keep the password file.** When it finishes, the terminal shows a line like
   `password for the lab: ab3k-9xqe-7tmd-p2wr`, also saved as `snap.img.password.txt`.
   That password is used if you pick the "no questions asked" install. The guided setup
   (Part 2) makes a new one for you and shows it on its first screen.
5. **Write the stick** with balenaEtcher as above, choosing `snap.img` from the SNAP folder.

</details>

## Part 2: set up the lab (on the spare computer, about 1 hour, mostly waiting)

Want to see each screen before you start? They are all in [docs/screens](screens/README.md).

1. **Plug in** the USB stick and the network cable, then turn the computer on.

2. **Start from the USB stick.** Right after switching on, press the boot menu key (usually **F12**, **F11**, **F8** or **Esc**; the screen often says which) and pick the USB stick.

3. **The SNAP menu appears**, with the SNAP logo at the top. After 10 seconds it starts
   **Install SNAP: guided setup**. (Press Enter to start it straight away.)

4. **Click through the setup screens.** Everything is already filled in, so you can just
   press **Next** on each one. They are, in order:
   1. **Host**: the computer's name, time zone, keyboard, and your **password**.
      Write the password down now. You need it to log in later.
   2. **Network**: the lab's networks. Add, change or remove them if you like.
   3. **Machines**: how many Ubuntu machines, and how big.
   4. **Firewall**: what each network may reach.
   5. **Review**: pick the disk to install on, tick the box that says you understand it
      will be erased, and click **Start installation**.

   If the first screen lists something to fix (like "Virtualization is turned off"), fix it
   in the computer's setup screen and click **Check again**.

5. **Wait.** A progress bar shows the install. The computer then restarts by itself and keeps
   setting up the lab. Leave the stick in; it knows what to do. The screen shows a checklist of
   the stages, the step it is on, and roughly how long is left (press **P** there to see the
   password again). When it says **Your lab is ready**, it is done.

**No questions at all?** Pick **Install with SNAP's settings, no questions asked** in the menu
instead. SNAP then shows the disk it will install on and erases it after 15 seconds (with more
than one disk, type the disk's number, press Enter, then type `ERASE` and press Enter). The
lab computer's screen shows the password (if you built the stick yourself, it is also in
`snap.img.password.txt`).

## Part 3: use your lab

1. The spare computer's screen says **Your lab is ready** and shows a web address like `https://192.168.1.50:8006`, the user name and the password. Type the address into the browser on your own computer.
2. Your browser warns that the connection is not private. That is expected for a new lab: choose **Advanced**, then **Continue** (or **Proceed**).
3. Log in with user name `root` and the password the lab computer's screen shows.

You will see the lab: the router (`snap-router`) and the machines `ubuntu-01` and `ubuntu-02`. To log in to an Ubuntu machine, open it and choose **Console**. The user name is `labuser` and the password is the same one.

## If something goes wrong

- **The computer does not start from the stick:** in its setup screen (often **F2** or **Delete** at start-up), turn on "USB boot". Also make sure "Virtualization" (Intel VT-x or AMD-V) is on.
- **SNAP says it found no internal disk:** the computer's disk is not detected. Check it in the setup screen.
- **The lab computer's screen says a stage "needs attention":** it shows what happened. Press **R** to
  try again; SNAP continues where it stopped. If it fails again, press **L** to see the log and note
  the last lines. Restarting the computer also tries again.
- **The setup screens say the install failed:** the screen shows what went wrong. Click
  **Try again**, or **Turn off computer** to fix the problem first (for example, a different disk).
- **Lost the password:** the lab computer's screen shows it: on the "ready" screen, or by pressing
  **P** while it is setting up. If you built the stick yourself and chose "no questions asked", it is
  in `snap.img.password.txt` next to `snap.img`.
