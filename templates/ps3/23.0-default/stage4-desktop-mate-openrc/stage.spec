source_subpath: @PLATFORM@/@REL_TYPE@/stage4-@SUB_ARCH@-desktop-openrc-@TIMESTAMP@
profile: default/linux/@BASE_ARCH@/23.0/desktop

use:
	ps3
	dist-kernel
	-qt5
	-qt6

packages: mate-base/mate
empty: /var/cache/distfiles
rm: /root/.bash_history
