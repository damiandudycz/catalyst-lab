source_subpath: @PLATFORM@/@REL_TYPE@/stage4-@SUB_ARCH@-base-openrc-@TIMESTAMP@
profile: default/linux/@BASE_ARCH@/23.0/desktop
compression_mode: pixz
releng_base: stages

stage4/use:
	ps3
	dist-kernel
	X

stage4/packages:
	x11-base/xorg-server
	x11-misc/lightdm

stage4/rcadd:
	display-manager|default

stage4/empty:
	/var/cache/distfiles

stage4/rm:
	/root/.bash_history
