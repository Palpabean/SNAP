#!/bin/bash
# Spike, phase 2: boot the ready-made system from a stand-in stick in QEMU (KVM),
# with an empty "computer disk" that must stay untouched. $1 = the dir from root.sh.
set -euo pipefail
out=$(realpath "$1")
cd "$out"
stick=stick.img
truncate -s 3G "$stick"
mkfs.ext4 -q -L SNAPLIVE "$stick"
mkdir -p mnt
sudo mount -o loop "$stick" mnt
sudo cp pve-root.squashfs mnt/
sudo umount mnt
truncate -s 8G computer.img
timeout 900 qemu-system-x86_64 -enable-kvm -cpu host -smp 4 -m 8192 -nographic \
  -kernel vmlinuz -initrd initrd.img -append "boot=snaplive console=ttyS0 quiet" \
  -drive file="$stick",format=raw,if=virtio,readonly=on \
  -drive file=computer.img,format=raw,if=virtio \
  -nic user,model=virtio-net-pci,hostfwd=tcp::8006-:8006 \
  > serial.log 2>&1 &
qemu=$!
for i in $(seq 180); do
  grep -q "SPIKE: done" serial.log && break
  kill -0 $qemu 2>/dev/null || break
  sleep 5
done
grep -a -E "SPIKE|Failed|failed|Kernel panic|panic" serial.log | head -60 || true
kill $qemu 2>/dev/null || true
wait $qemu 2>/dev/null || true
echo "== computer disk untouched?"
if cmp -s computer.img <(head -c "$(stat -c %s computer.img)" /dev/zero); then echo "UNTOUCHED"; else echo "WRITTEN"; fi
tail -40 serial.log
grep -q "SPIKE: done" serial.log
