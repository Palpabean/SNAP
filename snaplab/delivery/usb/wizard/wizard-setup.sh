# shellcheck shell=sh disable=SC2154
# (SC2154: $snap_root and the paths come from hook.sh.)
#
# SNAP: put the setup wizard in place of Proxmox's installer window
# (Design 0001, section 5.3). Sourced by hook.sh in the installer's start-up
# script when the SNAP menu started the guided setup (kernel option
# snap.wizard). The graphical environment then starts /.xinitrc, which opens
# the wizard instead of Proxmox's own installer.

snap_wizard_dir=$snap_root/usr/share/snap-wizard
mkdir -p "$snap_wizard_dir"
cp /snap/wizard/* "$snap_wizard_dir/"
cat >"$snap_root/.xinitrc" <<'EOF'
#!/bin/sh
# Written by SNAP: the setup wizard instead of Proxmox's installer window.
/usr/bin/xrdb -load /.Xdefaults 2>/dev/null
/usr/bin/X11/xsetroot -solid '#0F1720' 2>/dev/null
openbox &
# Errors go to the system console (the installer sends this session's own
# output to a hidden screen), so a failure shows in a serial log too.
/usr/bin/perl /usr/share/snap-wizard/snap-wizard.pl 2>>/dev/console
echo "SNAP wizard: window closed (exit code $?)" >/dev/console
EOF
chmod 755 "$snap_root/.xinitrc"
echo "SNAP: starting the setup wizard"
