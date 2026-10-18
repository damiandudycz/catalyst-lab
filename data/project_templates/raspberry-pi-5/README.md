# Raspberry Pi 5 template for Catalyst Lab

[Catalyst Lab](https://github.com/damiandudycz/catalyst-lab) project template that builds Gentoo for Raspberry Pi 5.
Select **From template** when creating a project in Catalyst Lab.

## Stages

```
stage1 ─┬─ stage3 ── stage4                          (optional console system)
        └─ stage3-desktop ─┬─ stage4-gnome           (if GNOME is selected)
                           └─ stage4-plasma          (if KDE Plasma is selected)
```

The console stage4 is optional and off by default. Select kernel packages in the stage package list as needed. It also
includes NetworkManager, SSH, NTP, cpupower and sudo.

## Options

| Option | Values |
|---|---|
| Init system | OpenRC, systemd (OpenRC services are enabled automatically; Plasma graphical login is enabled for both) |
| CPU target | ARM Cortex-A76 (cortex-a76), Generic baseline |
| Link-time optimization (LTO) | Off by default. Includes workarounds for packages that fail with LTO |
| Kernel | Select kernel packages in the stage package list; the template does not build or install a kernel itself |
| Accept all licenses | Off by default. When enabled, adds `ACCEPT_LICENSE="*"` to selected stage4 builds only |
| Installation ISO (experimental) | OpenRC only. Live ISO (`livecd-stage1`, `livecd-stage2` from console stage3) with installation tools. Raspberry Pi firmware boots from FAT partitions, not ISO images, so it's for testing |
| Live ISO with desktop (experimental) | OpenRC only. KDE Plasma live system like Gentoo LiveGUI (`livegui-stage1`, `livegui-stage2` from desktop stage3): SDDM logs user `gentoo` in to Plasma, basic applications (Konsole, Dolphin, Kate, Firefox, GParted) and installation tools. For testing, like the installation ISO |
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

Stage build identities include the selected init system. Switching between OpenRC and systemd keeps the same
stage layout and creates new stage IDs, preventing reuse of builds from the previous init system.
