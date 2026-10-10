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

mkdir -p "$(dirname "$DONE")"
touch "$DONE"
if [ -z "$ROOT" ]; then
    systemctl daemon-reload
    # --no-block: this script itself runs inside a first-boot unit.
    systemctl enable --now --no-block snap-resume.service
fi
say "Handoff complete: you can remove the USB now."
