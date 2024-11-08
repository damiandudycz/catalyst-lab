source_subpath: @REL_TYPE@/stage3-@SUB_ARCH@-desktop-openrc-@TIMESTAMP@

use:
	ps3
	dist-kernel

packages:
	x11-base/xorg-server
	x11-misc/lightdm

rcadd: display-manager|default
empty: /var/cache/distfiles
rm: /root/.bash_history
