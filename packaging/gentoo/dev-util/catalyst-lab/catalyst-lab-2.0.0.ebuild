# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

PYTHON_COMPAT=( python3_{11..14} )

inherit gnome2-utils meson optfeature python-single-r1 xdg

DESCRIPTION="GNOME application for building Gentoo stages, live images and systems with Catalyst"
HOMEPAGE="https://github.com/damiandudycz/catalyst-lab"

if [[ ${PV} == 9999 ]]; then
	inherit git-r3
	EGIT_REPO_URI="https://github.com/damiandudycz/catalyst-lab.git"
else
	SRC_URI="https://github.com/damiandudycz/catalyst-lab/archive/refs/tags/catalyst-lab-v${PV}.tar.gz -> ${P}.tar.gz"
	S="${WORKDIR}/catalyst-lab-catalyst-lab-v${PV}"
	KEYWORDS="~amd64 ~arm64"
fi

LICENSE="GPL-3+"
SLOT="0"
IUSE="+deploy"
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
	>=sys-apps/bubblewrap-0.11
	sys-apps/util-linux
	sys-auth/polkit
	sys-fs/squashfs-tools
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
"

src_install() {
	meson_src_install
	# Launcher is generated with Python found by meson (wrapper of python-single-r1).
	python_fix_shebang "${ED}"/usr/bin/catalystlab
	python_optimize "${ED}"/usr/share/catalystlab
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
