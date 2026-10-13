#!/bin/bash
# snap-firstboot.sh: the handoff from SNAP to CRACKLE (Design 0001, section 7.5).
#
# Baked into the Proxmox ISO at build time with
#   proxmox-auto-install-assistant prepare-iso --on-first-boot snap-firstboot.sh
# and run once by the installer's proxmox-first-boot service.
#
# It copies the payload from the stick's SNAPDATA partition to the host,
# verifies the copy against payload.sha256, and starts the snap-resume
# service, which runs CRACKLE, POP and BANG from the host's own disk.
#
# Expected SNAPDATA layout:
#   payload.sha256   sha256sum output for every other file, paths relative to SNAPDATA
#   snap.yaml        the lab configuration
#   bin/snap         the engine entry point
#   ...              everything else the later stages need
#
# For tests only: SNAP_ROOT prefixes every host path and skips systemctl,
# and SNAP_SOURCE is used in place of mounting the stick.
set -euo pipefail

ROOT="${SNAP_ROOT:-}"
LABEL=SNAPDATA
STATE_DIR="$ROOT/var/lib/snap"
LOG_DIR="$ROOT/var/log/snap"
UNIT="$ROOT/etc/systemd/system/snap-resume.service"
SCREEN_UNIT="$ROOT/etc/systemd/system/snap-screen.service"
# Progress markers, one per finished stage; the USB boot menu reads them.
DONE="$STATE_DIR/progress/snap.done"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/firstboot.log") 2>&1

say() {
    echo "snap: $*"
    if [ -z "$ROOT" ]; then echo "snap: $*" >/dev/console 2>/dev/null || true; fi
}

fail() {
    say "$*"
    say "Handoff failed. Restart with the USB stick plugged in to try again. Log: /var/log/snap/firstboot.log"
    exit 1
}

if [ -e "$DONE" ]; then
    say "handoff already complete"
    exit 0
fi

if [ -n "${SNAP_SOURCE:-}" ]; then
    src="$SNAP_SOURCE"
else
    dev="/dev/disk/by-label/$LABEL"
    for _ in $(seq 30); do
        [ -e "$dev" ] && break
        sleep 1
    done
    [ -e "$dev" ] || fail "the SNAP USB stick ($LABEL partition) was not found"
    src=/run/snap/usb
    mkdir -p "$src"
    mount -o ro "$dev" "$src" || fail "could not mount the $LABEL partition"
    trap 'umount "$src" 2>/dev/null || true' EXIT
fi

[ -f "$src/payload.sha256" ] || fail "payload.sha256 is missing from the USB stick"

# Copy to a staging directory, verify the copy, then move it into place, so a
# failed or interrupted copy never leaves a half-written state directory.
say "copying the lab payload from the USB stick"
staging="$STATE_DIR.partial"
rm -rf "$staging"
mkdir -p "$staging"
cp -R "$src/." "$staging/" || fail "copying from the USB stick failed"
(cd "$staging" && sha256sum --quiet --strict -c payload.sha256) ||
    fail "the copied files do not match their checksums; the USB stick may be damaged"
chmod -R go-rwx "$staging"
rm -rf "$STATE_DIR"
mv "$staging" "$STATE_DIR"

# A downloaded stick is the same for everyone, so it holds no password
# (password-at-install, from `snaplab build --release`): this computer picks
# its own, which the setup screen shows. The guided setup sets one itself.
if [ -e "$STATE_DIR/password-at-install" ] && [ ! -s "$ROOT/etc/snap/password" ]; then
    say "choosing this lab's own password (the screen shows it)"
    SNAP_ROOT="$ROOT" python3 - <<'PY' || fail "could not set this lab's password"
import json, os, secrets, subprocess

root = os.environ["SNAP_ROOT"]
alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # easy to read and type
password = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))
hashed = subprocess.run(
    ["openssl", "passwd", "-6", "-stdin"], input=password, capture_output=True, text=True, check=True
).stdout.strip()
os.makedirs(f"{root}/etc/snap", mode=0o700, exist_ok=True)
os.umask(0o077)
with open(f"{root}/etc/snap/password", "w") as f:
    f.write(password + "\n")
# For CRACKLE, POP and BANG: the router and the lab machines get it too.
path = f"{root}/etc/snap/wizard.json"
settings = json.load(open(path)) if os.path.exists(path) else {}
settings.setdefault("host", {})["root_password_hash"] = hashed
settings.setdefault("machines", {})["password_hash"] = hashed
with open(path, "w") as f:
    json.dump(settings, f, indent=2)
if not root:
    subprocess.run(["usermod", "-p", hashed, "root"], check=True)
PY
fi

mkdir -p "$(dirname "$UNIT")" "$ROOT/usr/local/bin"
ln -sf /var/lib/snap/bin/snap "$ROOT/usr/local/bin/snap"
cat >"$UNIT" <<'EOF'
[Unit]
Description=SNAP: continue lab setup (CRACKLE, POP, BANG)
Wants=network-online.target
After=network-online.target pve-cluster.service pveproxy.service
ConditionPathExists=/var/lib/snap/bin/snap
ConditionPathExists=!/var/lib/snap/progress/bang.done

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/var/lib/snap/bin/snap resume

[Install]
WantedBy=multi-user.target
EOF

# The setup screen on the computer's monitor (snaplab/screen.py), in place of
# the login prompt on the first console; Alt+F2 still gives one.
cat >"$SCREEN_UNIT" <<'EOF'
[Unit]
Description=SNAP: setup progress on the computer's screen
After=systemd-user-sessions.service
Conflicts=getty@tty1.service
ConditionPathExists=/var/lib/snap/bin/snap

[Service]
ExecStart=/var/lib/snap/bin/snap screen
StandardInput=tty
StandardOutput=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

mkdir -p "$(dirname "$DONE")"
touch "$DONE"
if [ -z "$ROOT" ]; then
    systemctl daemon-reload
    # --no-block: this script itself runs inside a first-boot unit.
    systemctl enable --now --no-block snap-resume.service snap-screen.service
fi
say "Handoff complete: you can remove the USB now."
