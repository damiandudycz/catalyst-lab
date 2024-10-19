source_subpath: @PLATFORM@/@REL_TYPE@/stage4-@SUB_ARCH@-desktop-openrc-@TIMESTAMP@
profile: default/linux/@BASE_ARCH@/23.0/desktop/gnome
compression_mode: pixz
releng_base: stages

#stage4/use:
#	dist-kernel
#	wayland
#	xwayland
#	X

packages:
	gnome-base/gnome

empty:
	/var/cache/distfiles

rm:
	/root/.bash_history
