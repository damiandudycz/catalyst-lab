target: stage4
version_stamp: base-openrc-@TIMESTAMP@
source_subpath: @PLATFORM@/@REL_TYPE@/stage3-@SUB_ARCH@-base-openrc-@TIMESTAMP@
profile: default/linux/@BASE_ARCH@/23.0
compression_mode: pixz
binrepo_path: @PLATFORM@/@REL_TYPE@

stage4/use:
	ps3
	dist-kernel

stage4/packages:
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

stage4/rcadd:
	ps3vram-swap|boot
	zram-init|boot
	dbus|default
        NetworkManager|default
	sysklogd|default
	ntpd|default
	ntp-client|default

stage4/empty:
	/var/cache/distfiles

stage4/rm:
	/root/.bash_history
