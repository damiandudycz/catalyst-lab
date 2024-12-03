EAPI=8

DESCRIPTION="An automation tool designed to streamline the process of building various Gentoo release stages and binhost packages."
HOMEPAGE="https://github.com/damiandudycz/catalyst-lab"
SRC_URI="https://github.com/damiandudycz/catalyst-lab/archive/refs/tags/v${PV}.zip"

KEYWORDS="amd64 arm64 ~ppc64"
LICENSE="GPL-2"
IUSE=""
SLOT="0"

DEPEND="
	dev-util/catalyst
	app-emulation/qemu[static-user]
	app-misc/yq
	sys-fs/squashfs-tools
	dev-vcs/git
	dev-vcs/git-lfs
"
RDEPEND="${DEPEND}"
BDEPEND=""

src_install() {
	dodir /usr/bin
	dobin "${WORKDIR}/catalyst-lab-${PV}/catalyst-lab"

	# Create the config file
	insinto /etc/catalyst-lab
	doins "${WORKDIR}/catalyst-lab-${PV}/catalyst-lab.conf"
}
