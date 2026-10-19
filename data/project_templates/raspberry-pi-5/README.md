# Raspberry Pi 5 template for Catalyst Lab

[Catalyst Lab](https://github.com/damiandudycz/catalyst-lab) project template that builds Gentoo for Raspberry Pi 5.
Select **From template** when creating a project in Catalyst Lab.

## Stages

```
stage1 ─┬─ stage3 ── stage4                          (optional console system)
        └─ stage3-desktop ─┬─ stage4-gnome           (if GNOME is selected)
                           └─ stage4-plasma          (if KDE Plasma is selected)
```

The console stage4 is optional and off by default. Stage4 builds include the prebuilt Raspberry Pi kernel and modules
from `sys-kernel/raspberrypi-image`. They also include NetworkManager, SSH, NTP, cpupower and sudo.

## Options

| Option | Values |
|---|---|
| Init systems | Toggle OpenRC, systemd, or both (at least one is required; OpenRC services are enabled automatically; Plasma graphical login is enabled for both) |
| CPU target | ARM Cortex-A76 (cortex-a76), Generic baseline |
| Link-time optimization (LTO) | Off by default. Includes workarounds for packages that fail with LTO |
| Kernel | Prebuilt Raspberry Pi kernel and modules (`sys-kernel/raspberrypi-image`) are installed in stage4 builds |
| Accept all licenses | Off by default. Required to make stage4 builds selectable because the Raspberry Pi kernel uses a restricted license. When enabled, accepts all licenses for selected stage4 builds through `package.license` |
| Installation ISO | OpenRC only. Live ISO (`livecd-stage1`, `livecd-stage2` from console stage3) with installation tools. Raspberry Pi firmware boots from FAT partitions, not ISO images, so this experimental option is for testing |
| Live ISO with desktop | OpenRC only. KDE Plasma live system like Gentoo LiveGUI (`livegui-stage1`, `livegui-stage2` from desktop stage3): SDDM logs user `gentoo` in to Plasma, basic applications (Konsole, Dolphin, Kate, Firefox, GParted) and installation tools. This experimental option is for testing |
| Desktops | GNOME, KDE Plasma (any of them, each gets its own stage4) |
| CPU frequency | 2.4 GHz (stock), 2.6 GHz, 2.8 GHz (needs good cooling) |
| Serial console | UART enabled in `config.txt` |
| zram swap | Disabled, 1, 2 or 4 GB |

Software groups, each applied to selected stage4 stages: Wi-Fi and Bluetooth, Raspberry Pi utilities, development
tools, Docker.

The hostname is generated from the name of the project.

## Desktop boot

Desktop stage4 builds include an Xorg OutputClass rule selecting the VC4 display controller as the primary GPU. This
avoids selecting the V3D render-only device when DRM card numbers change. ISO stages are unchanged.

Plasma stage4 starts with a graphical login screen: systemd builds enable SDDM and set `graphical.target` offline
inside the build, while OpenRC builds enable `display-manager` with SDDM selected in its configuration.

Existing projects need **Update template** and a rebuild of the Plasma stage4 to include these changes in new images.

## Notes

- `cmdline.txt` (root device and kernel arguments) isn't created by the template, it depends on the device the system
  is deployed to and is created when deploying.
- Booting from NVMe or USB also needs the boot order in the EEPROM of Raspberry Pi set to try these devices
  (`rpi-eeprom-config`).

## Format

See [Project templates](https://github.com/damiandudycz/catalyst-lab/blob/main/docs/project-templates.md).

Selecting both init systems creates parallel OpenRC and systemd stage graphs. Stage build identities include the
selected init system, preventing reuse of builds across variants.
