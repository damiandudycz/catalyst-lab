profile: default/linux/@BASE_ARCH@/23.0
source_subpath: @PLATFORM@/@RELEASE@/stage3-@SUB_ARCH@-base-openrc-@TIMESTAMP@

use:
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
	dev-embedded/raspberrypi-utils
	sys-firmware/raspberrypi-wifi-ucode
	sys-kernel/raspberrypi-image

rcadd:
	zram-init|boot
	dbus|default
        NetworkManager|default
	bluetooth|default
	sysklogd|default
	ntpd|default
	ntp-client|default
	sshd|default

empty: /var/cache/distfiles
rm: /root/.bash_history
