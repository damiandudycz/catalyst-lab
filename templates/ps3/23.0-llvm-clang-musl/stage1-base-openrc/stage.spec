profile: ps3:default/linux/@BASE_ARCH@/23.0/musl/llvm
source_subpath: @PLATFORM@/@REL_TYPE@/stage4-@SUB_ARCH@-seed-llvm-musl-clang-base-openrc-@TIMESTAMP@
update_seed: yes
update_seed_command: --update --deep --newuse --usepkg --buildpkg @system @world
