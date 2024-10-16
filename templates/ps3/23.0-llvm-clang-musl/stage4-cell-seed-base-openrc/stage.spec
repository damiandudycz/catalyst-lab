# Stage4 is build from normal gcc stage3 seed, and required tools are installed. This includes llvm, clang and musl.
# This stage will be later used as a seed for building stage1-llvm-clang
# Custom pkgcache_path is used so that builds created using gcc are not mixed with later clang builds.

target: stage4
version_stamp: seed-base-openrc-@TIMESTAMP@
# Profile for LLVM/MUSL
#profile: ps3:default/linux/ppc64/23.0/musl/llvm
profile: default/linux/@BASE_ARCH@/23.0/musl
source_subpath: @BASE_ARCH@/stage3-@BASE_ARCH@-openrc-@TIMESTAMP@
compression_mode: pixz
releng_base: stages

# chost="powerpc64-unknown-linux-gnu" # TODO: Should this be set as -gnu for this first stage? (to differenciate from release config). Probably not, as we build this on musl profile.

pkgcache_path: @PKGCACHE_BASE_PATH@/@PLATFORM@/@REL_TYPE@-seed

stage4/packages:
	llvm
	clang
	musl

# TODO: Remove GCC?
# TODO: For later stages change chost for -musl?
