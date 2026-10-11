# shellcheck shell=sh disable=SC2154,SC3045
# (SC2154: $cdrom comes from /init. SC3045: busybox's shell has read -t.)
#
# SNAP: choose the install disk on the target computer (Design 0001, section 5.2).
#
# Sourced by the Proxmox installer's start-up script (/init in its initrd)
# just before it starts the installer, when the stick is built without a
# target disk. Runs in the installer's busybox shell; $cdrom is the stick.
#
#   one internal disk:   shown with a countdown, then used
#   several:             a numbered list; the chosen disk must be confirmed
#   none:                stop and say so
#
# The answer file names a placeholder disk. Only after a disk is chosen does
# the installer get an answer file naming it, so if anything here goes wrong
# the installer finds no such disk and stops before erasing anything.

snap_placeholder=snap-choose-at-boot
# Overridable for tests (tests/test_disk_prompt.py).
snap_answer=${SNAP_ANSWER:-/mnt/answer.toml}
snap_root=${SNAP_INSTALLER_ROOT:-/mnt/.installer-mp}
snap_sys=${SNAP_SYS_BLOCK:-/sys/block}

snap_disk_list() {
    snap_boot=${cdrom#/dev/}
    for d in "$snap_sys"/*; do
        n=${d##*/}
        case "$n" in
            loop*|ram*|zram*|sr*|fd*|md*|dm-*|nbd*|mmcblk*boot*) continue ;;
        esac
        [ "$n" = "$snap_boot" ] && continue
        [ "$(cat "$d/size" 2>/dev/null || echo 0)" -gt 0 ] || continue
        # Never USB: that is the stick itself, card readers and external drives.
        case "$(readlink -f "$d/device" 2>/dev/null)" in
            */usb*) continue ;;
        esac
        echo "$n"
    done
}

snap_describe() {
    d=$snap_sys/$1
    gb=$(( $(cat "$d/size") * 512 / 1000000000 ))
    model=$(cat "$d/device/model" 2>/dev/null | sed 's/  */ /g; s/^ //; s/ $//')
    echo "$1  ${model:-unknown model}  ${gb} GB"
}

snap_use_disk() {
    mkdir -p "$snap_root/etc/snap"
    sed "s/$snap_placeholder/$1/" "$snap_answer" >"$snap_root/etc/snap/answer.toml"
    fetch=$snap_root/usr/bin/proxmox-fetch-answer
    mv "$fetch" "$fetch.orig"
    printf '%s\n' '#!/bin/sh' '# SNAP: the answer file with the disk chosen at boot.' \
        'exec cat /etc/snap/answer.toml' >"$fetch"
    chmod 755 "$fetch"
    echo
    echo "SNAP: installing Proxmox VE on $(snap_describe "$1")"
    sleep 2
}

snap_menu() {
    while :; do
        echo
        echo "SNAP: choose the disk to install Proxmox VE on. EVERYTHING ON IT WILL BE ERASED."
        i=0
        for n in $snap_disks; do
            i=$((i + 1))
            echo "  $i) $(snap_describe "$n")"
        done
        echo "  0) cancel and power off"
        printf 'Disk number: '
        read -r pick || pick=
        [ "$pick" = 0 ] && { echo "Cancelled; nothing was changed."; sleep 3; poweroff -f; }
        i=0
        chosen=
        for n in $snap_disks; do
            i=$((i + 1))
            [ "$pick" = "$i" ] && chosen=$n
        done
        [ -n "$chosen" ] || { echo "Please type one of the numbers shown."; continue; }
        printf 'Type ERASE to erase %s: ' "$(snap_describe "$chosen")"
        read -r confirm || confirm=
        if [ "$confirm" = ERASE ]; then
            snap_use_disk "$chosen"
            return
        fi
        echo "Not confirmed; nothing was changed."
    done
}

if grep -q "$snap_placeholder" "$snap_answer" 2>/dev/null; then
    snap_disks=$(snap_disk_list)
    snap_count=$(echo "$snap_disks" | grep -c .)
    echo
    echo "=================================================================="
    echo " SNAP: choosing where to install"
    echo "=================================================================="
    if [ "$snap_count" -eq 0 ]; then
        debugsh_err_reboot "SNAP found no internal disk to install on (USB drives are never used)."
    elif [ "$snap_count" -eq 1 ]; then
        echo "SNAP will install Proxmox VE on: $(snap_describe "$snap_disks")"
        echo "EVERYTHING ON THIS DISK WILL BE ERASED."
        echo "Starting in 15 seconds. Press Enter to choose differently or cancel."
        if read -r -t 15 _; then
            snap_menu
        else
            snap_use_disk "$snap_disks"
        fi
    else
        snap_menu
    fi
fi
