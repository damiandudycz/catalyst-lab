source_subpath: @PLATFORM@/@RELEASE@/stage4-@SUB_ARCH@-desktop-openrc-@TIMESTAMP@

use:
	ps3
	dist-kernel
	-qt5
	-qt6

packages: xfce-base/xfce4-meta
empty: /var/cache/distfiles
rm: /root/.bash_history
