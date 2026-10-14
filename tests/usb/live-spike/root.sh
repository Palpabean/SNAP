#!/bin/bash
# Spike, phase 2: build a ready-made Proxmox system from the ISO, as its installer
# does, and pack it to run from a stick. $1 = the extracted ISO, $2 = output dir.
# Needs root (chroot, mounts).
set -euxo pipefail
iso=$(realpath "$1")
out=$(realpath -m "$2")
root=$out/root
mkdir -p "$out"
rm -rf "$root"
unsquashfs -q -d "$root" "$iso/pve-base.squashfs"

for d in proc sys dev dev/pts; do mkdir -p "$root/$d"; done
mount -t proc proc "$root/proc"
mount -t sysfs sys "$root/sys"
mount --bind /dev "$root/dev"
mount --bind /dev/pts "$root/dev/pts"
cleanup() { for d in dev/pts dev sys proc; do umount -l "$root/$d" 2>/dev/null || true; done; }
trap cleanup EXIT

# Services must not start inside the chroot.
printf '#!/bin/sh\nexit 101\n' > "$root/usr/sbin/policy-rc.d"
chmod +x "$root/usr/sbin/policy-rc.d"
echo snap > "$root/etc/hostname"
printf '127.0.0.1 localhost\n10.0.2.15 snap.lab.internal snap\n' > "$root/etc/hosts"
cp /etc/resolv.conf "$root/etc/resolv.conf" || true

mkdir -p "$root/tmp/debs"
cp "$iso"/proxmox/packages/*.deb "$root/tmp/debs/"
ls "$root/tmp/debs" | wc -l
cat > "$root/tmp/preseed" <<'EOF'
postfix postfix/main_mailer_type select Local only
postfix postfix/mailname string snap.lab.internal
grub-pc grub-pc/install_devices multiselect
grub-pc grub-pc/install_devices_empty boolean true
EOF
chroot "$root" debconf-set-selections /tmp/preseed
export DEBIAN_FRONTEND=noninteractive
# As Proxmox's installer: unpack everything, then configure.
chroot "$root" sh -c 'dpkg --force-depends --no-triggers --force-unsafe-io --force-confold --unpack /tmp/debs/*.deb' > "$out/unpack.log" 2>&1 || { tail -50 "$out/unpack.log"; exit 1; }
chroot "$root" dpkg --force-confold --configure -a > "$out/configure.log" 2>&1 || { tail -80 "$out/configure.log"; }
chroot "$root" dpkg --audit | head -40 || true
rm -rf "$root/tmp/debs" "$root/tmp/preseed"

ls -la "$root/boot"
kver=$(ls "$root/lib/modules" | head -1)
echo "kernel: $kver"

# Booting from a stick: the initrd finds SNAPLIVE, mounts pve-root.squashfs
# read-only with a RAM layer on top (boot=snaplive).
cat > "$root/etc/initramfs-tools/scripts/snaplive" <<'EOF'
mountroot() {
    modprobe -q squashfs; modprobe -q overlay; modprobe -q loop
    for i in $(seq 60); do
        [ -e /dev/disk/by-label/SNAPLIVE ] && break
        sleep 1
    done
    mkdir -p /run/snaplive/stick /run/snaplive/lower /run/snaplive/rw
    mount -o ro /dev/disk/by-label/SNAPLIVE /run/snaplive/stick || panic "SNAPLIVE not found"
    mount -o ro,loop -t squashfs /run/snaplive/stick/pve-root.squashfs /run/snaplive/lower || panic "squashfs"
    mount -t tmpfs -o mode=0755 tmpfs /run/snaplive/rw
    mkdir -p /run/snaplive/rw/upper /run/snaplive/rw/work
    mount -t overlay -o lowerdir=/run/snaplive/lower,upperdir=/run/snaplive/rw/upper,workdir=/run/snaplive/rw/work overlay "${rootmnt}" || panic "overlay"
    for m in stick lower rw; do mkdir -p "${rootmnt}/run/snaplive/$m"; mount --move /run/snaplive/$m "${rootmnt}/run/snaplive/$m" 2>/dev/null || true; done
}
EOF
printf 'squashfs\noverlay\nloop\nvirtio_blk\nvirtio_pci\nusb_storage\nuas\next4\n' >> "$root/etc/initramfs-tools/modules"
chroot "$root" update-initramfs -c -k "$kver" > "$out/initramfs.log" 2>&1 || { tail -30 "$out/initramfs.log"; exit 1; }

# The spike's stand-in for the network and identity work done at each boot.
cat > "$root/usr/local/sbin/snap-live-net" <<'EOF'
#!/bin/sh
nic=$(ls /sys/class/net | grep -v -E '^(lo|vmbr|tap|fwbr|fwln|fwpr|veth)' | head -1)
cat > /etc/network/interfaces <<EON
auto lo
iface lo inet loopback

iface $nic inet manual

auto vmbr0
iface vmbr0 inet static
    address 10.0.2.15/24
    gateway 10.0.2.2
    bridge-ports $nic
    bridge-stp off
    bridge-fd 0
EON
systemd-machine-id-setup
ssh-keygen -A
EOF
chmod +x "$root/usr/local/sbin/snap-live-net"
cat > "$root/etc/systemd/system/snap-live-net.service" <<'EOF'
[Unit]
Description=SNAP spike: network and identity
DefaultDependencies=no
Before=networking.service pve-cluster.service ssh.service
After=local-fs.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/snap-live-net
[Install]
WantedBy=sysinit.target
EOF
cat > "$root/usr/local/sbin/snap-spike-check" <<'EOF'
#!/bin/bash
exec > /dev/ttyS0 2>&1
sleep 20
echo "SPIKE: services"; systemctl is-active pve-cluster pvedaemon pveproxy pvestatd
echo "SPIKE: pvesm"; pvesm status
pvecm updatecerts --force >/dev/null 2>&1 && echo "SPIKE: certs ok"
qm create 900 --name spike --memory 256 --net0 virtio,bridge=vmbr0 --scsi0 local:1,format=raw && echo "SPIKE: created"
qm start 900 && sleep 5 && qm status 900
curl -sk -o /dev/null -w "SPIKE: web %{http_code}\n" https://127.0.0.1:8006/
df -h / | tail -1; free -m | head -2
echo "SPIKE: done"
EOF
chmod +x "$root/usr/local/sbin/snap-spike-check"
cat > "$root/etc/systemd/system/snap-spike-check.service" <<'EOF'
[Unit]
After=pveproxy.service pve-cluster.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/snap-spike-check
[Install]
WantedBy=multi-user.target
EOF
chroot "$root" systemctl enable snap-live-net.service snap-spike-check.service serial-getty@ttyS0.service
echo 'root:spike-pass-123' | chroot "$root" chpasswd
rm -f "$root/etc/machine-id" "$root/var/lib/dbus/machine-id" "$root"/etc/ssh/ssh_host_*
touch "$root/etc/machine-id"
rm -f "$root/usr/sbin/policy-rc.d"
cleanup
trap - EXIT

cp "$root/boot/vmlinuz-$kver" "$out/vmlinuz"
cp "$root/boot/initrd.img-$kver" "$out/initrd.img"
du -sh "$root"
du -sh "$root"/usr/lib/firmware 2>/dev/null || true
mksquashfs "$root" "$out/pve-root.squashfs" -comp xz -Xbcj x86 -b 1M -noappend -quiet -e boot
ls -la "$out"
echo "pve-root.squashfs: $(( $(stat -c %s "$out/pve-root.squashfs") / 1048576 )) MiB"
