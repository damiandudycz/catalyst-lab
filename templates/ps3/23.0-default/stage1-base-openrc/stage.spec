profile: default/linux/@BASE_ARCH@/23.0
source_subpath: @BASE_ARCH@/stage3-@BASE_ARCH@-openrc-@TIMESTAMP@
update_seed: yes
update_seed_command: --update --deep --newuse --usepkg --buildpkg @system @world
