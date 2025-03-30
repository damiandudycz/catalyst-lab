profile: gentoo-amd64-profiles:default/linux/@BASE_ARCH@/23.0/musl/llvm/desktop
source_subpath: @REL_TYPE@/stage3-@SUB_ARCH@-desktop-openrc-@TIMESTAMP@
repos: [git]https://github.com/damiandudycz/gentoo-amd64-profile.git

use:
	networkmanager

packages:
	=dev-libs/libdex-0.8.1
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
	sys-kernel/linux-firmware
	dev-lang/rust

rcadd:
	zram-init|boot
	dbus|default
        NetworkManager|default
	sysklogd|default
	ntpd|default
	ntp-client|default
	sshd|default

unmerge:
	dev-lang/rust-bin

empty: /var/cache/distfiles
rm: /root/.bash_history
