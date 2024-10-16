# Stage1 is build from normal gcc stage3 seed, and required tools are installed using fsscript. This includes llvm, clang and musl.

target: stage1
version_stamp: base-openrc-@TIMESTAMP@
# Profile for LLVM/MUSL
#profile: ps3:default/linux/ppc64/23.0/musl/llvm
profile: default/linux/@BASE_ARCH@/23.0
source_subpath: @BASE_ARCH@/stage3-@BASE_ARCH@-openrc-@TIMESTAMP@
compression_mode: pixz
update_seed: yes
update_seed_command: --update --deep --newuse --usepkg --buildpkg @system @world
releng_base: stages
