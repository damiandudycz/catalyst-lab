profile: default/linux/@BASE_ARCH@/23.0/desktop/gnome
source_subpath: @PLATFORM@/@RELEASE@/stage4-@SUB_ARCH@-desktop-openrc-@TIMESTAMP@

use:
	networkmanager
	pulseaudio
	bluetooth
#	-qt5
#	-qt6

packages: gnome-base/gnome
empty: /var/cache/distfiles
rm: /root/.bash_history
