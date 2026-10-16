# PlayStation 3

Gentoo for PlayStation 3, based on the configuration from the first version of Catalyst Lab.

```
stage1 ─┬─ stage3 ── stage4                       (optional console system)
        └─ stage3-desktop ── stage4-desktop-xfce    (optional, OpenRC)

iso-seed-stage1-openrc ── iso-seed-stage3-openrc ── livecd-stage1 ── livecd-stage2
```

- Stages are built for Cell CPU (`cell` subarch, AltiVec), optionally with LTO.
- The console stage4 is optional and off by default. It includes PS3 kernel (`sys-kernel/gentoo-kernel-ps3`), video memory swap (`ps3vram-swap`) and `ps3pf_utils`,
  NetworkManager, SSH and NTP. Optional zram swap and development tools.
- PS3 packages come from [ps3-gentoo-overlay](https://github.com/damiandudycz/ps3-gentoo-overlay), which is added to
  Catalyst Lab when the project is created.

- Installation ISO (optional, OpenRC): live system with PS3 kernel built by Catalyst (`boot/kernel: ps3`, distribution
  kernel with dracut live initramfs), PS3 Gentoo installer and tools of Gentoo minimal installation ISO. petitboot
  loads it with `yaboot.conf` on the ISO, which uses volume label `Gentoo-ps3`.

Requires Catalyst Lab 2.0.0 or newer.

ISO builds use a dedicated OpenRC stage1 and stage3 seed chain, so switching the project's init system cannot
reuse systemd builds as ISO seeds. After updating an existing project, the new seed stages must be built once.
