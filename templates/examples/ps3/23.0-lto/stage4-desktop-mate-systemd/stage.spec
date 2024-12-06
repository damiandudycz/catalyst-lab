source_subpath: @REL_TYPE@/stage4-@SUB_ARCH@-desktop-systemd-@TIMESTAMP@

use:
	ps3
	dist-kernel
	-qt5
	-qt6

packages: mate-base/mate
empty: /var/cache/distfiles
rm: /root/.bash_history
