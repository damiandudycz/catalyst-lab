# ASUS ROG Ally / Ally X Gentoo template

Catalyst Lab project template for Gentoo on the ASUS ROG Ally and Ally X. It provides OpenRC or systemd stages, AMD
Zen 4 optimization, and optional console, GNOME and KDE Plasma stage4 builds, an optional customized GNOME stage, and
optional installer and Plasma live ISOs. Stage4 builds are off by default. An AVX-512 restriction is available for
cross-architecture builds under QEMU; leave it off for native builds.

The desktop stages include AMD graphics, handheld input, wireless, and initramfs Portage settings carried forward
from the original ROG Ally example in Catalyst Lab's `version-2` branch. The system kernel and firmware are supplied
by `gentoo-kernel-bin` and `linux-firmware`.
