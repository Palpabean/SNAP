# shellcheck shell=sh
#
# SNAP's hook in the Proxmox installer's start-up script (/init in its initrd),
# run just before the installer starts (snaplab/delivery/usb/initrd.py).
#
#   guided setup (kernel option snap.wizard): the graphical setup wizard
#   no questions asked: the disk prompt, when no disk was chosen at build time

# shellcheck disable=SC2034,SC1091  # used by the scripts sourced below, which live in the initrd
snap_root=${SNAP_INSTALLER_ROOT:-/mnt/.installer-mp}
case " $(cat /proc/cmdline) " in
    *" snap.wizard"*) . /snap/wizard/wizard-setup.sh ;;
    *) . /snap/choose-disk.sh ;;
esac
