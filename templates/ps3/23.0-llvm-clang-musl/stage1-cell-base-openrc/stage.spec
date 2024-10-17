target: stage1
version_stamp: base-openrc-@TIMESTAMP@
profile: default/linux/@BASE_ARCH@/23.0/musl/llvm
source_subpath: @BASE_ARCH@/@REL_TYPE@/stage4-@SUB_ARCH@-seed-base-openrc-@TIMESTAMP@
compression_mode: pixz
update_seed: yes
update_seed_command: --update --deep --newuse --usepkg --buildpkg @system @world
releng_base: stages
