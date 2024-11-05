source_subpath: @REL_TYPE@/stage3-@SUB_ARCH@-desktop-openrc-@TIMESTAMP@

use:
	networkmanager
	pulseaudio
	bluetooth

packages:
	x11-base/xorg-server
	gnome-base/gdm

rcadd: display-manager|default
empty: /var/cache/distfiles
rm: /root/.bash_history
