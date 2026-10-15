#!/bin/bash
# Configure the KDE Plasma live desktop in the stage2 image.
set -euo pipefail
echo "Running ROG Ally live desktop setup"
source /etc/profile
env-update

mkdir -p /mnt/gentoo

if [ -f /etc/conf.d/display-manager ]; then
    sed -e '/^DISPLAYMANAGER=/s/.*/DISPLAYMANAGER="sddm"/' -i /etc/conf.d/display-manager
fi

mkdir -p /etc/NetworkManager
printf '[main]\nplugins=keyfile\nhostname-mode=none\n' > /etc/NetworkManager/NetworkManager.conf
printf '[Autologin]\nUser=gentoo\nSession=plasma.desktop\nRelogin=true\n' > /etc/sddm.conf

if [ -d /home/gentoo ]; then
    mkdir -p /home/gentoo/.config /home/gentoo/Desktop
    printf '[Daemon]\nAutolock=false\nLockOnResume=false\n' > /home/gentoo/.config/kscreenlockerrc
    printf '[Wallet]\nEnabled=false\n' > /home/gentoo/.config/kwalletrc
    chown -R gentoo:users /home/gentoo
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl enable sddm.service || true
fi
