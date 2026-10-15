#!/bin/bash
# Live ISO with desktop (Raspberry Pi 5): prepares live system like Gentoo LiveGUI (releases/specs/amd64/livegui of
# releng), with KDE Plasma started for user gentoo. Run by catalyst as livecd/fsscript of livecd-stage2.
set -e
echo "Running LiveGUI stage2 fsscript"
source /etc/profile
env-update

# Installation target, like on installation ISO.
mkdir -p /mnt/gentoo

# SDDM starts Plasma.
sed -e '/^DISPLAYMANAGER=/s/.*/DISPLAYMANAGER="sddm"/' -i /etc/conf.d/display-manager

# NetworkManager doesn't change hostname (it breaks xauth).
mkdir -p /etc/NetworkManager
printf '[main]\nplugins=keyfile\nhostname-mode=none\n' > /etc/NetworkManager/NetworkManager.conf

# User gentoo is logged in to Plasma automatically.
printf '[Autologin]\nUser=gentoo\nSession=plasma.desktop\nRelogin=true\n' > /etc/sddm.conf

# Desktop of user gentoo: no screen lock and wallet in live system.
if [ -d /home/gentoo ]; then
    mkdir -p /home/gentoo/.config /home/gentoo/Desktop
    printf '[Daemon]\nAutolock=false\nLockOnResume=false\n' > /home/gentoo/.config/kscreenlockerrc
    printf '[Wallet]\nEnabled=false\n' > /home/gentoo/.config/kwalletrc
    chown -R gentoo:users /home/gentoo # Primary group of live user is users (as catalyst creates it).
fi
