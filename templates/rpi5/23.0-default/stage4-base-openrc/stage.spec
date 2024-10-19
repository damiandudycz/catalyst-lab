version_stamp: base-openrc-@TIMESTAMP@
source_subpath: @PLATFORM@/@REL_TYPE@/stage3-@SUB_ARCH@-base-openrc-@TIMESTAMP@
profile: default/linux/@BASE_ARCH@/23.0
compression_mode: pixz
binrepo_path: @PLATFORM@/@REL_TYPE@
releng_base: stages

#stage4/use:
#	dist-kernel

stage4/packages:
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

stage4/rcadd:
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
