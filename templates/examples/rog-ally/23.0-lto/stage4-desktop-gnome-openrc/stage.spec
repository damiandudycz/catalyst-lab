profile: default/linux/@BASE_ARCH@/23.0/desktop/gnome
source_subpath: @REL_TYPE@/stage3-@SUB_ARCH@-desktop-openrc-@TIMESTAMP@

use:
	-qt5 -qt6
	networkmanager
	pulseaudio
	bluetooth

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
	virtual/dist-kernel #	sys-kernel/gentoo-kernel
	gnome-base/gnome
	sys-kernel/linux-firmware

rcadd:
	zram-init|boot
	dbus|default
        NetworkManager|default
	bluetooth|default
	sysklogd|default
	ntpd|default
	ntp-client|default
	sshd|default
	display-manager|default

empty: /var/cache/distfiles
rm: /root/.bash_history
