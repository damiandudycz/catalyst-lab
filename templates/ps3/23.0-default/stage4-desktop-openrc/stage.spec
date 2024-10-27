profile: default/linux/@BASE_ARCH@/23.0/desktop
source_subpath: @REL_TYPE@/stage4-@SUB_ARCH@-openrc-@TIMESTAMP@

use:
	ps3
	dist-kernel

packages:
	x11-base/xorg-server
	x11-misc/lightdm

rcadd: display-manager|default
empty: /var/cache/distfiles
rm: /root/.bash_history
