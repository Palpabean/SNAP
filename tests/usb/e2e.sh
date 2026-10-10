#!/bin/bash
# End-to-end test of stage 1 (SNAP) with the real Proxmox ISO, in QEMU with KVM.
#
#   tests/usb/e2e.sh snap.img bios|uefi
#
# Build the stick with --serial-console so the installer's output lands in the logs.
#
# Pass 1 boots the stick next to an empty SATA disk whose serial matches
# examples/snap.example.yaml; the unattended install must finish and restart.
# Pass 2 boots the stick again: its menu must detect the install and boot it,
# and the first-boot handoff must copy the payload. The target disk is then
# inspected with LVM on the host.
set -euo pipefail

IMG="$1"
FIRMWARE="${2:-bios}"
WORK="${WORK:-$(mktemp -d)}"
mkdir -p "$WORK"
SERIAL=S4EWNX0R123456
OVMF_CODE=/usr/share/OVMF/OVMF_CODE_4M.fd
OVMF_VARS=/usr/share/OVMF/OVMF_VARS_4M.fd

[ -e /dev/kvm ] || { echo "e2e: /dev/kvm is missing; a full install without KVM takes hours" >&2; exit 1; }

cp "$IMG" "$WORK/stick.img"
truncate -s 32G "$WORK/target.img"

qemu() {
    local log="$1" limit="$2"
    shift 2
    local fw=()
    if [ "$FIRMWARE" = uefi ]; then
        cp "$OVMF_VARS" "$WORK/vars.fd"
        fw=(-drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE" -drive "if=pflash,format=raw,file=$WORK/vars.fd")
    fi
    timeout "$limit" qemu-system-x86_64 -enable-kvm -cpu host -smp 2 -m 4096 \
        -display none -serial "file:$log" -no-reboot "${fw[@]}" \
        -nic user,model=virtio-net-pci \
        -device qemu-xhci -drive "file=$WORK/stick.img,format=raw,if=none,id=stick" \
        -device usb-storage,drive=stick,bootindex=0 \
        -device ahci,id=ahci -drive "file=$WORK/target.img,format=raw,if=none,id=target" \
        -device "ide-hd,drive=target,bus=ahci.0,serial=$SERIAL,bootindex=1" \
        "$@"
}

echo "== pass 1: unattended install ($FIRMWARE)"
# The SNAP menu shows on screen and serial; its default entry starts the install.
start=$(date +%s)
if ! qemu "$WORK/pass1.log" 30m; then
    echo "e2e: the install did not finish and restart within 30 minutes" >&2
    tr -d '\r' < "$WORK/pass1.log" | tail -n 150 >&2 || true
    exit 1
fi
echo "install finished in $(( $(date +%s) - start ))s"

echo "== pass 2: boot the stick again, expect it to start the installed system"
qemu "$WORK/pass2.log" 6m || true    # the installed system keeps running; stop it after the handoff
tr -d '\r' < "$WORK/pass2.log" | grep -a "SNAP" || true
grep -aq "SNAP: starting the installed Proxmox VE." "$WORK/pass2.log" || {
    echo "e2e: the stick's menu did not detect the installed system" >&2
    exit 1
}

echo "== inspect the target disk"
loop=$(sudo losetup -f --show -P "$WORK/target.img")
trap 'sudo umount "$WORK/mnt" 2>/dev/null || true; sudo vgchange -q -an pve >/dev/null 2>&1 || true; sudo losetup -d "$loop"' EXIT
sudo vgscan -q >/dev/null
sudo vgchange -q -ay pve
mkdir -p "$WORK/mnt"
sudo mount -o ro /dev/pve/root "$WORK/mnt"
sudo cat "$WORK/mnt/var/log/snap/firstboot.log"
sudo test -e "$WORK/mnt/var/lib/snap/progress/snap.done"
sudo cmp "$WORK/mnt/var/lib/snap/snap.yaml" examples/snap.example.yaml
sudo test -L "$WORK/mnt/etc/systemd/system/multi-user.target.wants/snap-resume.service"
sudo journalctl -D "$WORK/mnt/var/log/journal" --no-pager 2>/dev/null | grep -a "SNAP:" || true
echo "e2e: OK ($FIRMWARE): installed unattended, stick booted the install, handoff complete"
