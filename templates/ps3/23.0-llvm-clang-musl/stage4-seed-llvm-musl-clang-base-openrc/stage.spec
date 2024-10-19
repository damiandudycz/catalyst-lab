# Builds seed used by stage1.
# Installs llvm, clang and musl on top of stage3-ppc64-musl-hardened-openrc remote seed.

profile: default/linux/@BASE_ARCH@/23.0/musl
source_subpath: @BASE_ARCH@/stage3-@BASE_ARCH@-musl-hardened-openrc-@TIMESTAMP@
pkgcache_path: @PKGCACHE_BASE_PATH@/@PLATFORM@/@REL_TYPE@-seed

# Keep this seed outside of release directory.
rel_type: @PLATFORM@

use: ps3 dist-kernel lto
packages: llvm clang
empty: /var/cache/distfiles
rm: /root/.bash_history
