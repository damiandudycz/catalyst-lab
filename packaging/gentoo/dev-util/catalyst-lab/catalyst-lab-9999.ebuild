# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

GO_OPTIONAL=1
PYTHON_COMPAT=( python3_{11..14} )

inherit go-module gnome2-utils meson optfeature python-single-r1 xdg

DESCRIPTION="GNOME application for building Gentoo stages, live images and systems with Catalyst"
HOMEPAGE="https://github.com/damiandudycz/catalyst-lab"

LIMA_VERSION="2.2.1"
if [[ ${PV} == 9999 ]]; then
	inherit git-r3
	EGIT_REPO_URI="https://github.com/damiandudycz/catalyst-lab.git"
else
	SRC_URI="https://github.com/damiandudycz/catalyst-lab/archive/refs/tags/catalyst-lab-v${PV}.tar.gz -> ${P}.tar.gz"
	KEYWORDS="~amd64 ~arm64"
fi
SRC_URI+=" lima? (
	https://github.com/lima-vm/lima/archive/refs/tags/v${LIMA_VERSION}.tar.gz -> lima-${LIMA_VERSION}.tar.gz
	https://github.com/lima-vm/lima/releases/download/v${LIMA_VERSION}/lima-${LIMA_VERSION}-go-mod-vendor.tar.gz
)"
if [[ ${PV} != 9999 ]]; then
	S="${WORKDIR}/catalyst-lab-catalyst-lab-v${PV}"
fi

LICENSE="GPL-3+ lima? ( Apache-2.0 BSD BSD-2 ISC MIT MPL-2.0 PostgreSQL Python-2.0 X11 ZLIB )"
SLOT="0"
IUSE="+deploy lima"
REQUIRED_USE="${PYTHON_REQUIRED_USE}"

# Toolsets run in bubblewrap namespaces, mounted from squashfs images. Without unprivileged user namespaces, root
# helper is started with pkexec.
RDEPEND="
	${PYTHON_DEPS}
	$(python_gen_cond_dep '
		dev-python/pygobject:3[${PYTHON_USEDEP}]
		dev-python/requests[${PYTHON_USEDEP}]
	')
	gui-libs/gtk:4[introspection]
	>=gui-libs/libadwaita-1.7:1[introspection]
	dev-vcs/git
	x11-themes/hicolor-icon-theme
	>=sys-apps/bubblewrap-0.11
	sys-apps/util-linux
	sys-auth/polkit
	sys-fs/squashfs-tools
	lima? (
		app-emulation/qemu
		net-misc/openssh
	)
	deploy? (
		app-arch/xz-utils
		dev-libs/libisoburn
		net-misc/openssh
		sys-fs/dosfstools
		sys-fs/e2fsprogs
		sys-fs/mtools
	)
"
BDEPEND="
	${PYTHON_DEPS}
	dev-util/glib-utils
	sys-devel/gettext
	lima? ( >=dev-lang/go-1.26.0 app-arch/gzip )
"

src_unpack() {
	if [[ ${PV} == 9999 ]]; then
		git-r3_src_unpack
	else
		unpack "${P}.tar.gz"
	fi
	if use lima; then
		unpack "lima-${LIMA_VERSION}.tar.gz"
		local lima_src="${WORKDIR}/lima-${LIMA_VERSION}"
		tar -xf "${DISTDIR}/lima-${LIMA_VERSION}-go-mod-vendor.tar.gz" -C "${lima_src}" || die
		go-env_set_compile_environment
	fi
}

src_compile() {
	meson_src_compile
	use lima || return

	local lima_src="${WORKDIR}/lima-${LIMA_VERSION}"
	local lima_ldflags="-s -w -X github.com/lima-vm/lima/v2/pkg/version.Version=v${LIMA_VERSION}"
	local goarch guestagent_arch
	case ${ARCH} in
		amd64)
			goarch="amd64"
			guestagent_arch="x86_64"
			;;
		arm64)
			goarch="arm64"
			guestagent_arch="aarch64"
			;;
		*) die "Lima runner is not supported on ${ARCH}" ;;
	esac

	cd "${lima_src}" || die
	go-env_set_compile_environment
	export GOFLAGS="-mod=vendor"
	export GOPROXY="off"
	export GOTOOLCHAIN="local"
	mkdir -p _output/bin _output/share/lima/templates || die
	ego build -trimpath -ldflags "${lima_ldflags}" -o _output/bin/limactl ./cmd/limactl
	CGO_ENABLED=0 GOOS=linux GOARCH="${goarch}" ego build -trimpath -ldflags "${lima_ldflags}" \
		-o "_output/share/lima/lima-guestagent.Linux-${guestagent_arch}" ./cmd/lima-guestagent
	gzip -n "_output/share/lima/lima-guestagent.Linux-${guestagent_arch}" || die

	local template
	for template in templates/*; do
		[[ ${template##*/} == experimental ]] && continue
		cp -aL "${template}" _output/share/lima/templates/ || die
	done
}

src_install() {
	meson_src_install
	# Launcher is generated with Python found by meson (wrapper of python-single-r1).
	python_fix_shebang "${ED}"/usr/bin/catalystlab
	python_optimize "${ED}"/usr/share/catalystlab

	if use lima; then
		local lima_src="${WORKDIR}/lima-${LIMA_VERSION}"
		insinto /usr/libexec/catalyst-lab/lima/bin
		doins "${lima_src}"/_output/bin/limactl
		fperms 0755 /usr/libexec/catalyst-lab/lima/bin/limactl
		insinto /usr/libexec/catalyst-lab/lima/share/lima
		doins -r "${lima_src}"/_output/share/lima/*

		insinto "/usr/share/licenses/${PF}/lima"
		doins "${lima_src}"/LICENSE "${lima_src}"/NOTICE
		local license_file relative_path target_dir
		while IFS= read -r -d '' license_file; do
			relative_path=${license_file#"${lima_src}"/vendor/}
			target_dir=$(dirname "${relative_path}")
			insinto "/usr/share/licenses/${PF}/lima/vendor/${target_dir}"
			newins "${license_file}" "${relative_path##*/}"
		done < <(find "${lima_src}"/vendor -type f \( -iname 'LICENSE*' -o -iname 'COPYING*' -o -iname 'NOTICE*' \) -print0)
	fi
}

pkg_postinst() {
	xdg_pkg_postinst
	gnome2_schemas_update
	optfeature "deploying builds of other architectures to disks of this computer (binfmt)" "app-emulation/qemu[static-user]"
	elog "Builds run without root when unprivileged user namespaces are enabled and your user has entries in"
	elog "/etc/subuid and /etc/subgid (for example: usermod --add-subuids 100000-165535 --add-subgids 100000-165535 \$USER)."
	elog "Otherwise operations ask for root password through polkit (pkexec)."
}

pkg_postrm() {
	xdg_pkg_postrm
	gnome2_schemas_update
}
