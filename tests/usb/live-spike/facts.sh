#!/bin/bash
# Spike, phase 1: what the volatile lab would boot. $1 = the extracted ISO.
set -u
iso=$1
echo "== installer kernel"
file "$iso/boot/linux26"
echo "== installer initrd"
file "$iso/boot/initrd.img"
mkdir -p initrd && (cd initrd && { zstd -dc ../"$iso"/boot/initrd.img 2>/dev/null || gzip -dc ../"$iso"/boot/initrd.img; } | cpio -idm 2>/dev/null)
ls initrd initrd/lib/modules 2>/dev/null
echo "== installer /init"
cat initrd/init
echo "== initrd modules: overlay, squashfs, loop"
find initrd/lib/modules -name 'overlay*' -o -name 'squashfs*' -o -name 'loop*' | head
echo "== pve-base: kernels and modules"
unsquashfs -l "$iso/pve-base.squashfs" 'boot/*' 2>/dev/null | head -30
unsquashfs -l "$iso/pve-base.squashfs" 2>/dev/null | grep -E '^squashfs-root/lib/modules/[^/]+$' | head
unsquashfs -l "$iso/pve-base.squashfs" 2>/dev/null | grep -cE 'overlay\.ko' || true
echo "== pve-base size"
ls -la "$iso"/*.squashfs
echo "== pve-installer: what it adds"
unsquashfs -l "$iso/pve-installer.squashfs" 2>/dev/null | grep -E 'usr/bin/proxmox|sbin/unconfigured' | head -20
echo "== grub entries"
sed -n 1,200p "$iso/boot/grub/grub.cfg"
