source_subpath: @REL_TYPE@/stage3-@SUB_ARCH@-openrc-@TIMESTAMP@

use:
	ps3
	dist-kernel

packages:
	app-admin/sudo
	app-admin/sysklogd
        app-eselect/eselect-repository
	app-misc/ps3pf_utils
        app-portage/gentoolkit
        dev-vcs/git
        net-misc/networkmanager
	net-misc/ntp
	sys-apps/ps3vram-swap
	sys-block/zram-init
        sys-devel/distcc
	sys-kernel/gentoo-kernel-ps3
        sys-kernel/linux-headers

rcadd:
	ps3vram-swap|boot
	zram-init|boot
	dbus|default
        NetworkManager|default
	sysklogd|default
	ntpd|default
	ntp-client|default

empty: /var/cache/distfiles
rm: /root/.bash_history
