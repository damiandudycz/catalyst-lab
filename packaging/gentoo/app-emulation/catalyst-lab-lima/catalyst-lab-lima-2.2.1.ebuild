# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

GO_OPTIONAL=1
inherit go-module

DESCRIPTION="Private Lima runtime for Catalyst Lab VM runners"
HOMEPAGE="https://lima-vm.io/ https://github.com/lima-vm/lima"
SRC_URI="
	https://github.com/lima-vm/lima/archive/refs/tags/v${PV}.tar.gz -> lima-${PV}.tar.gz
	https://github.com/lima-vm/lima/releases/download/v${PV}/lima-${PV}-go-mod-vendor.tar.gz
"
S="${WORKDIR}/lima-${PV}"

LICENSE="Apache-2.0 BSD BSD-2 ISC MIT MPL-2.0 PostgreSQL Python-2.0 X11 ZLIB"
SLOT="0"
KEYWORDS="~amd64 ~arm64"

BDEPEND="
	>=dev-lang/go-1.26.0
	sys-devel/make
"

src_unpack() {
	default
	tar -xf "${DISTDIR}/lima-${PV}-go-mod-vendor.tar.gz" -C "${S}" || die
	go-env_set_compile_environment
}

src_compile() {
	go-env_set_compile_environment
	export GOFLAGS="-mod=vendor"
	export GOPROXY="off"
	emake VERSION="v${PV}" limactl native-guestagent templates
}

src_install() {
	cd "${S}" || die
	local prefix="/usr/libexec/catalyst-lab/lima"
	insinto "${prefix}/bin"
	doins _output/bin/limactl
	fperms 0755 "${prefix}/bin/limactl"
	insinto "${prefix}/share/lima"
	doins -r _output/share/lima/*

	insinto "/usr/share/licenses/${PF}"
	doins LICENSE NOTICE
	local license_file relative_path target_dir
	while IFS= read -r -d '' license_file; do
		relative_path=${license_file#vendor/}
		target_dir=$(dirname "${relative_path}")
		insinto "/usr/share/licenses/${PF}/vendor/${target_dir}"
		newins "${license_file}" "$(basename "${license_file}")"
	done < <(find vendor -type f \( -iname 'LICENSE*' -o -iname 'COPYING*' -o -iname 'NOTICE*' \) -print0)
}
