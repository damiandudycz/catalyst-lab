source_subpath: @REL_TYPE@/stage3-@SUB_ARCH@-openrc-@TIMESTAMP@

use: dist-kernel

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
	sys-kernel/gentoo-kernel
	sys-kernel/linux-firmware

rcadd:
	zram-init|boot
	dbus|default
        NetworkManager|default
	sysklogd|default
	ntpd|default
	ntp-client|default
	sshd|default

empty: /var/cache/distfiles
rm: /root/.bash_history
