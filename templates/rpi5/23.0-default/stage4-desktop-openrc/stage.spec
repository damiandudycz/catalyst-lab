source_subpath: @PLATFORM@/@REL_TYPE@/stage4-@SUB_ARCH@-base-openrc-@TIMESTAMP@
profile: default/linux/@BASE_ARCH@/23.0/desktop

use:
	networkmanager
	pulseaudio

packages:
	x11-base/xorg-server
	gnome-base/gdm

rcadd: display-manager|default
empty: /var/cache/distfiles
rm: /root/.bash_history
