version_stamp: @TIMESTAMP@
source_subpath: @PLATFORM@/@REL_TYPE@/stage3-@SUB_ARCH@-base-openrc-@TIMESTAMP@
target: livecd-stage1
profile: default/linux/@BASE_ARCH@/23.0
compression_mode: pixz

livecd/use:
 	ps3
	compile-locales
	fbcon
	socks5
	unicode
	xml

livecd/packages:
	sys-apps/ps3-gentoo-installer
	sys-apps/ps3vram-swap
	sys-block/zram-init
 	app-portage/gentoolkit
 	net-misc/ntp
	sys-block/zram-init
	net-misc/networkmanager
	app-accessibility/brltty
	app-admin/pwgen
	app-arch/lbzip2
	app-arch/pigz
	app-arch/zstd
	app-crypt/gnupg
	app-misc/livecd-tools
	app-portage/mirrorselect
	app-shells/bash-completion
	app-shells/gentoo-bashcomp
	net-analyzer/tcptraceroute
	net-analyzer/traceroute
	net-misc/dhcpcd
	net-misc/iputils
	net-misc/openssh
	net-misc/rdate
	net-wireless/iw
	net-wireless/iwd
	net-wireless/wireless-tools
	net-wireless/wpa_supplicant
	sys-apps/busybox
	sys-apps/ethtool
	sys-apps/fxload
	sys-apps/gptfdisk
	sys-apps/hdparm
	sys-apps/ibm-powerpc-utils
	sys-apps/ipmitool
	sys-apps/iproute2
	sys-apps/lsvpd
	sys-apps/memtester
	sys-apps/merge-usr
	sys-apps/ppc64-diag
	sys-apps/sdparm
	sys-apps/usbutils
	sys-auth/ssh-import-id
	sys-block/parted
	sys-fs/bcache-tools
	sys-fs/btrfs-progs
	sys-fs/cryptsetup
	sys-fs/dosfstools
	sys-fs/e2fsprogs
	sys-fs/f2fs-tools
	sys-fs/iprutils
	sys-fs/lsscsi
	sys-fs/lvm2
	sys-fs/mdadm
	sys-fs/mtd-utils
	sys-fs/sysfsutils
	sys-fs/xfsprogs
	sys-libs/gpm
	sys-process/lsof
	www-client/links
