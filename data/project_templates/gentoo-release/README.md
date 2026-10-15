# Gentoo release

Stages built from official Gentoo Releng specifications. Select stage4, minimal installation ISO, or (on amd64/OpenRC)
LiveGUI ISO under Extra stages; all are optional and off by default.

- Architectures: amd64, arm64, arm (ARMv7-A, ARMv6, ARMv5TE, ARMv4T, hard float), ppc, ppc64, ppc64le (POWER9),
  s390x, sparc (SPARC64, SPARC), x86 (i686, i686 with SSE math, i486).
- CPU target: generic baseline by default. amd64 offers x86-64-v2/v3/v4 and AMD Zen 1 through Zen 5 targets; arm64 offers Armv8.2-A/8.4-A/9-A,
  ppc64 offers POWER8/9, the existing POWER9 option remains available for ppc64le, and s390x offers z13/z15. The optional
  AVX-512 restriction appears for x86-64-v4 and Zen 4/5, for cross-building under QEMU; leave it off for native builds.
  More specific targets require matching hardware; x86-64-v4 requires AVX-512.
- Link-time optimization (LTO): optional, stage1 is built with `-flto` added to selected CPU flags, later stages inherit it.
- Init system: OpenRC or systemd. Network: NetworkManager, dhcpcd (OpenRC) or systemd-networkd (systemd).
- Installation ISO: where releng has installcd specs, built from OpenRC stage3.
- LiveGUI ISO: amd64/OpenRC only, based on the Releng desktop stage3 and LiveGUI specs, including its Releng kernel settings.
- Software of stage4: kernel and firmware, GRUB, sudo, vim, administration tools. Stage4 is optional.
