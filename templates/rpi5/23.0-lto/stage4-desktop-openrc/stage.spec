source_subpath: @REL_TYPE@/stage3-@SUB_ARCH@-desktop-openrc-@TIMESTAMP@

packages:
	app-admin/sudo
        app-admin/sysklogd
        app-eselect/eselect-repository
        app-portage/gentoolkit
        dev-vcs/git
        net-misc/networkmanager
        net-misc/ntp
        sys-block/zram-init
        sys-devel/distcc
        sys-kernel/linux-headers
        dev-embedded/raspberrypi-utils
        sys-firmware/raspberrypi-wifi-ucode
        sys-kernel/raspberrypi-image
        sys-power/cpupower
	x11-base/xorg-server
	gnome-base/gdm

rcadd:
      	zram-init|boot
        cpupower|boot
        dbus|default
        NetworkManager|default
        bluetooth|default
        sysklogd|default
        ntpd|default
        ntp-client|default
        sshd|default
	display-manager|default

use:
	networkmanager
	pulseaudio
	bluetooth

empty: /var/cache/distfiles
rm: /root/.bash_history
