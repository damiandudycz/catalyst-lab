profile: gentoo-amd64-profiles:default/linux/@BASE_ARCH@/23.0/musl/llvm/desktop/gnome
source_subpath: @REL_TYPE@/stage4-@SUB_ARCH@-desktop-openrc-@TIMESTAMP@
repos: [git]https://github.com/damiandudycz/gentoo-amd64-profile.git

use:
	-qt5 -qt6
	networkmanager
	pulseaudio
	bluetooth

packages:
	gnome-base/gnome

rcadd:
	display-manager|default

empty: /var/cache/distfiles
rm: /root/.bash_history
