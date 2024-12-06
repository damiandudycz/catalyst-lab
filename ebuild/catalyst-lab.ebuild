EAPI=8

DESCRIPTION="An automation tool designed to streamline the process of building various Gentoo release stages and binhost packages."
HOMEPAGE="https://github.com/damiandudycz/catalyst-lab"
SRC_URI="https://github.com/damiandudycz/catalyst-lab/archive/refs/tags/v${PV}.zip -> catalyst-lab-${PV}.zip"

KEYWORDS="~amd64 ~arm64"
LICENSE="GPL-2"
IUSE="qemu git binhost"
SLOT="0"

DEPEND="
	dev-util/catalyst
	app-misc/yq
	binhost? ( sys-fs/squashfs-tools )
	qemu? ( app-emulation/qemu[static-user] )
	git? ( dev-vcs/git dev-vcs/git-lfs )
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
