target: stage1
version_stamp: base-openrc-@TIMESTAMP@
profile: default/linux/@BASE_ARCH@/23.0
source_subpath: @BASE_ARCH@/stage3-@BASE_ARCH@-openrc-@TIMESTAMP@
compression_mode: pixz
update_seed: yes
update_seed_command: --update --deep --newuse --usepkg --buildpkg @system @world
releng_base: stages
