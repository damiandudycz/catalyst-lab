source_subpath: @PLATFORM@/@REL_TYPE@/stage4-@SUB_ARCH@-base-openrc-@TIMESTAMP@
profile: default/linux/@BASE_ARCH@/23.0/desktop
compression_mode: pixz
releng_base: stages

use:
	ps3
	dist-kernel
	X

packages:
	x11-base/xorg-server
	x11-misc/lightdm

rcadd:
	display-manager|default

empty:
	/var/cache/distfiles

rm:
	/root/.bash_history