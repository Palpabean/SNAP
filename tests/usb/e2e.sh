#!/bin/bash
# End-to-end test with the real Proxmox ISO, in QEMU with KVM: stage 1 (SNAP),
# then CRACKLE and POP on the installed host, whose lab VMs run nested.
#
#   tests/usb/e2e.sh snap.img bios|uefi text|wizard
#
# Build the stick from tests/usb/e2e.yaml with --serial-console and
# --unattended text|wizard so the installer's and the engine's output land in
# the logs and nobody has to press anything.
#
# Pass 1 boots the stick next to an empty SATA disk and must install and
# restart. text: the stick's disk prompt must offer the disk and use it after
# its countdown. wizard: the graphical setup wizard fills in its defaults,
# installs, and saves its choices on the new disk; its screens are saved as
# pictures in $WORK/screens.
# Pass 2 boots the stick again: its menu must detect the install and boot it,
# and the first-boot handoff must copy the payload, then CRACKLE and POP run.
# The target disk is then inspected with LVM on the host.
set -euo pipefail

IMG="$1"
FIRMWARE="${2:-bios}"
SETUP="${3:-text}"
WORK="${WORK:-$(mktemp -d)}"
mkdir -p "$WORK"
SERIAL=S4EWNX0R123456
OVMF_CODE=/usr/share/OVMF/OVMF_CODE_4M.fd
OVMF_VARS=/usr/share/OVMF/OVMF_VARS_4M.fd

[ -e /dev/kvm ] || { echo "e2e: /dev/kvm is missing; a full install without KVM takes hours" >&2; exit 1; }

cp "$IMG" "$WORK/stick.img"
truncate -s 64G "$WORK/target.img"

qemu() {
    local log="$1" limit="$2"
    shift 2
    local fw=()
    if [ "$FIRMWARE" = uefi ]; then
        cp "$OVMF_VARS" "$WORK/vars.fd"
        fw=(-drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE" -drive "if=pflash,format=raw,file=$WORK/vars.fd")
    fi
    timeout "$limit" qemu-system-x86_64 -enable-kvm -cpu host -smp 4 -m 8192 \
        -display none -serial "file:$log" -no-reboot -pidfile "$WORK/qemu.pid" "${fw[@]}" \
        -monitor "unix:$WORK/monitor.sock,server,nowait" \
        -nic user,model=virtio-net-pci \
        -device qemu-xhci -drive "file=$WORK/stick.img,format=raw,if=none,id=stick" \
        -device usb-storage,drive=stick,bootindex=0 \
        -device ahci,id=ahci -drive "file=$WORK/target.img,format=raw,if=none,id=target" \
        -device "ide-hd,drive=target,bus=ahci.0,serial=$SERIAL,bootindex=1" \
        "$@"
}

# A picture of the screen every 30 seconds while pass 1 runs (the wizard is graphical).
screens() {
    mkdir -p "$WORK/screens"
    local n=0
    while sleep 30; do
        n=$((n + 1))
        python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); s.sendall(sys.argv[2].encode())' \
            "$WORK/monitor.sock" "screendump $WORK/screens/$(printf %03d $n).ppm"$'\n' 2>/dev/null || true
    done
}

echo "== pass 1: unattended install ($FIRMWARE, $SETUP)"
# The SNAP menu shows on screen and serial; its default entry starts the install.
start=$(date +%s)
screens &
shooter=$!
if ! qemu "$WORK/pass1.log" 30m; then
    kill "$shooter" 2>/dev/null || true
    echo "e2e: the install did not finish and restart within 30 minutes" >&2
    tr -d '\r' < "$WORK/pass1.log" | tail -n 150 >&2 || true
    exit 1
fi
kill "$shooter" 2>/dev/null || true
for ppm in "$WORK"/screens/*.ppm; do
    [ -s "$ppm" ] && pnmtopng "$ppm" > "${ppm%.ppm}.png" 2>/dev/null && rm -f "$ppm"
done
echo "install finished in $(( $(date +%s) - start ))s"
if [ "$SETUP" = wizard ]; then
    # The wizard ran the installer itself and reported each step on the console.
    grep -a "SNAP wizard: P" "$WORK/pass1.log" | tail -n 5 || true
    grep -aq "SNAP wizard: DONE" "$WORK/pass1.log" || {
        echo "e2e: the setup wizard did not report a finished install" >&2
        tr -d '\r' < "$WORK/pass1.log" | tail -n 80 >&2 || true
        exit 1
    }
else
    # Plug-and-play: no disk was named, so the stick asked; with one disk it counts down and uses it.
    grep -a "SNAP will install Proxmox VE on:" "$WORK/pass1.log" || {
        echo "e2e: the stick did not offer the only disk" >&2
        exit 1
    }
fi

echo "== pass 2: boot the stick again; it must start the installed system, which runs CRACKLE and POP"
qemu "$WORK/pass2.log" 110m &
result=timeout
for _ in $(seq 6600); do
    sleep 1
    if grep -aq "SNAP: POP (router, networks and lab machines) done" "$WORK/pass2.log"; then result=finished; break; fi
    if grep -aqE "SNAP: .*(failed|missing)" "$WORK/pass2.log"; then result=failed; break; fi
    [ -s "$WORK/qemu.pid" ] && ! kill -0 "$(cat "$WORK/qemu.pid")" 2>/dev/null && { result=exited; break; }
done
# Shut the host down cleanly (ACPI power button), so everything it wrote reaches the disk.
python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); s.sendall(b"system_powerdown\n")' \
    "$WORK/monitor.sock" 2>/dev/null || true
for _ in $(seq 180); do
    kill -0 "$(cat "$WORK/qemu.pid")" 2>/dev/null || break
    sleep 1
done
if kill "$(cat "$WORK/qemu.pid" 2>/dev/null)" 2>/dev/null; then
    echo "e2e: the host did not power off within 3 minutes; stopped it"
fi
wait || true
tr -d '\r' < "$WORK/pass2.log" | grep -a "SNAP" || true
echo "CRACKLE and POP result: $result"

echo "== inspect the target disk"
loop=$(sudo losetup -f --show -P "$WORK/target.img")
trap 'sudo umount "$WORK/mnt" 2>/dev/null || true; sudo vgchange -q -an pve >/dev/null 2>&1 || true; sudo losetup -d "$loop"' EXIT
sudo vgscan -q >/dev/null
sudo vgchange -q -ay pve
mkdir -p "$WORK/mnt"
sudo mount -o ro /dev/pve/root "$WORK/mnt"
sudo cat "$WORK/mnt/var/log/snap/firstboot.log"
sudo test -e "$WORK/mnt/var/lib/snap/progress/snap.done"
sudo cmp "$WORK/mnt/var/lib/snap/snap.yaml" tests/usb/e2e.yaml
sudo test -L "$WORK/mnt/etc/systemd/system/multi-user.target.wants/snap-resume.service"
sudo test -L "$WORK/mnt/etc/systemd/system/multi-user.target.wants/snap-screen.service"
if [ "$SETUP" = wizard ]; then
    # The wizard's choices, kept for CRACKLE and POP, with the password readable by root only.
    sudo python3 -m json.tool "$WORK/mnt/etc/snap/wizard.json" | head -n 20
    sudo test -s "$WORK/mnt/etc/snap/password"
    [ "$(sudo stat -c %a "$WORK/mnt/etc/snap/password")" = 600 ]
fi
sudo journalctl -D "$WORK/mnt/var/log/journal" --no-pager 2>/dev/null | grep -a "SNAP:" || true
echo "== engine log"
sudo cat "$WORK/mnt/var/log/snap/engine.log" || true
sudo cat "$WORK/mnt/var/lib/snap/state.json" || true
echo "== router console"
sudo tail -c 20000 "$WORK/mnt/var/log/snap/router-console.log" 2>/dev/null | tr -d '\r' || true

grep -aq "SNAP: starting the installed Proxmox VE." "$WORK/pass2.log" || {
    echo "e2e: the stick's menu did not detect the installed system" >&2
    exit 1
}
[ "$result" = finished ] || { echo "e2e: CRACKLE and POP did not finish ($result)" >&2; exit 1; }
sudo test -e "$WORK/mnt/var/lib/snap/progress/crackle.done"
# The automation tools CRACKLE installs (design decision D7).
sudo test -x "$WORK/mnt/usr/local/bin/tofu"
sudo grep -q 'filesystem_mirror' "$WORK/mnt/root/.tofurc"
sudo grep -A1 -E '^Package: (ansible-core|python3-httpx)$' "$WORK/mnt/var/lib/dpkg/status" | grep -c 'install ok installed' | grep -qx 2
sudo test -d "$WORK/mnt/usr/share/ansible/collections/ansible_collections/oxlorg/opnsense"
sudo test -s "$WORK/mnt/var/lib/snap/secrets/proxmox-api-token"
# POP: the router imported its config, and the check from inside the lab passed.
sudo test -e "$WORK/mnt/var/lib/snap/progress/pop.done"
sudo test -s "$WORK/mnt/var/lib/snap/pop/router-imported"
sudo test -s "$WORK/mnt/var/lib/snap/pop/verified.sha256"
echo "e2e: OK ($FIRMWARE, $SETUP): installed unattended, stick booted the install, handoff, CRACKLE and POP complete"
