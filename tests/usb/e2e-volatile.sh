#!/bin/bash
# End-to-end test of the volatile stick with the real Proxmox ISO, in QEMU with KVM.
#
#   tests/usb/e2e-volatile.sh snap-volatile.img bios|uefi
#
# Build it from tests/usb/e2e.yaml with --volatile --serial-console (and --release,
# so the lab computer picks its own password, as a downloaded stick does).
#
# One pass: the stick boots its ready-made Proxmox system with a RAM layer on top,
# SNAP hands over and CRACKLE, POP and BANG build the lab (VMs nested) until
# "Your lab is ready". The computer's own disk is attached the whole time and must
# still be all zeros afterwards: a volatile lab writes nothing to it. Pictures of the
# screen (boot menu, start-up screen, SNAP's setup screen) go to $WORK/screens.
set -euo pipefail

IMG="$1"
FIRMWARE="${2:-uefi}"
WORK="${WORK:-$(mktemp -d)}"
mkdir -p "$WORK/screens"
OVMF_CODE=/usr/share/OVMF/OVMF_CODE_4M.fd
OVMF_VARS=/usr/share/OVMF/OVMF_VARS_4M.fd

[ -e /dev/kvm ] || { echo "e2e: /dev/kvm is missing" >&2; exit 1; }

cp "$IMG" "$WORK/stick.img"
truncate -s 32G "$WORK/disk.img"  # the computer's own disk: must stay untouched

fw=()
if [ "$FIRMWARE" = uefi ]; then
    cp "$OVMF_VARS" "$WORK/vars.fd"
    fw=(-drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE" -drive "if=pflash,format=raw,file=$WORK/vars.fd")
fi

monitor() {
    python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); s.sendall(sys.argv[2].encode())' \
        "$WORK/monitor.sock" "$1"$'\n' 2>/dev/null || true
}

echo "== the volatile lab ($FIRMWARE)"
start=$(date +%s)
timeout 110m qemu-system-x86_64 -enable-kvm -cpu host -smp 4 -m 10240 \
    -display none -serial "file:$WORK/volatile.log" -no-reboot -pidfile "$WORK/qemu.pid" "${fw[@]}" \
    -monitor "unix:$WORK/monitor.sock,server,nowait" \
    -nic user,model=virtio-net-pci \
    -device qemu-xhci -drive "file=$WORK/stick.img,format=raw,if=none,id=stick" \
    -device usb-storage,drive=stick,bootindex=0 \
    -device ahci,id=ahci -drive "file=$WORK/disk.img,format=raw,if=none,id=disk" \
    -device "ide-hd,drive=disk,bus=ahci.0,bootindex=1" &

# Pictures: often while it starts (boot menu, start-up screen), then every minute.
(
    n=0
    while sleep 5; do
        n=$((n + 1))
        [ $n -gt 60 ] && sleep 55
        monitor "screendump $WORK/screens/$(printf %03d $n).ppm"
    done
) &
shooter=$!

result=timeout
for _ in $(seq 6600); do
    sleep 1
    if grep -aq "SNAP: Your lab is ready." "$WORK/volatile.log"; then result=finished; break; fi
    if grep -aqE "SNAP: .*(failed|missing|cannot start)" "$WORK/volatile.log"; then result=failed; break; fi
    [ -s "$WORK/qemu.pid" ] && ! kill -0 "$(cat "$WORK/qemu.pid")" 2>/dev/null && { result=exited; break; }
done
sleep 10
monitor "screendump $WORK/screens/ready.ppm"
sleep 2
kill "$shooter" 2>/dev/null || true
monitor "system_powerdown"
for _ in $(seq 120); do
    kill -0 "$(cat "$WORK/qemu.pid" 2>/dev/null)" 2>/dev/null || break
    sleep 1
done
kill "$(cat "$WORK/qemu.pid" 2>/dev/null)" 2>/dev/null || true
wait || true
for ppm in "$WORK"/screens/*.ppm; do
    [ -s "$ppm" ] && pnmtopng "$ppm" > "${ppm%.ppm}.png" 2>/dev/null && rm -f "$ppm"
done
echo "result: $result after $(( $(date +%s) - start ))s"
tr -d '\r' < "$WORK/volatile.log" | grep -a -E "SNAP|snap-volatile" | tail -n 80 || true

echo "== the computer's own disk"
size=$(stat -c %s "$WORK/disk.img")
if cmp -s -n "$size" "$WORK/disk.img" /dev/zero; then
    echo "untouched: all $size bytes are still zero"
else
    echo "e2e: the volatile lab wrote to the computer's disk" >&2
    exit 1
fi

if [ "$result" != finished ]; then
    echo "e2e: the volatile lab did not reach 'Your lab is ready' ($result)" >&2
    tr -d '\r' < "$WORK/volatile.log" | tail -n 200 >&2 || true
    exit 1
fi
grep -aq "SNAP: starting the volatile lab from the USB stick." "$WORK/volatile.log" || {
    echo "e2e: the boot menu did not start the volatile lab" >&2
    exit 1
}
grep -aq "This lab runs from the USB stick: leave it in." "$WORK/volatile.log" || {
    echo "e2e: the handoff did not report a volatile lab" >&2
    exit 1
}
echo "e2e: OK (volatile, $FIRMWARE): ran from the stick to 'Your lab is ready'; the computer's disk is untouched"
