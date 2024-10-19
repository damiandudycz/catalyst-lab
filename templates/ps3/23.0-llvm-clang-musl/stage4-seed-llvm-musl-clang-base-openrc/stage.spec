# Builds seed used by stage1.
# Installs llvm, clang and musl on top of stage3-ppc64-musl-hardened-openrc remote seed.

version_stamp: seed-llvm-musl-clang-base-openrc-@TIMESTAMP@
profile: default/linux/@BASE_ARCH@/23.0/musl
source_subpath: @BASE_ARCH@/stage3-@BASE_ARCH@-musl-hardened-openrc-@TIMESTAMP@
compression_mode: pixz
binrepo_path: @PLATFORM@/@REL_TYPE@
releng_base: stages
pkgcache_path: @PKGCACHE_BASE_PATH@/@PLATFORM@/@REL_TYPE@-seed

# Keep this seed outside of release directory.
#rel_type: @PLATFORM@

stage4/use:
	ps3
	dist-kernel
	lto

stage4/packages:
	llvm
	clang
#	musl

stage4/empty:
	/var/cache/distfiles

stage4/rm:
	/root/.bash_history
