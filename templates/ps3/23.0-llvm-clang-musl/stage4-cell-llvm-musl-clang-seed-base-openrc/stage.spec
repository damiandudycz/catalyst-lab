# Builds seed used by stage1.
# Installs llvm, clang and musl on top of stage3-ppc64-musl-hardened-openrc remote seed.

target: stage4
version_stamp: llvm-musl-clang-seed-base-openrc-@TIMESTAMP@
profile: default/linux/@BASE_ARCH@/23.0/musl
source_subpath: @BASE_ARCH@/stage3-@BASE_ARCH@-musl-hardened-openrc-@TIMESTAMP@
compression_mode: pixz
binrepo_path: @PLATFORM@/@REL_TYPE@
releng_base: stages
pkgcache_path: @PKGCACHE_BASE_PATH@/@PLATFORM@/@REL_TYPE@-seed

# Keep this seed outside of release directory.
rel_type: @SUB_ARCH@

stage4/use:
	ps3
	dist-kernel

stage4/packages:
	llvm
	clang
	musl

stage4/empty:
	/var/cache/distfiles

stage4/rm:
	/root/.bash_history

# chost="powerpc64-unknown-linux-gnu" # TODO: Should this be set as -gnu for this first stage? (to differenciate from release config). Probably not, as we build this on musl profile.

# TODO: Remove GCC?
# TODO: For later stages change chost for -musl?
