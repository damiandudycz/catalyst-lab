from __future__ import annotations

# ------------------------------------------------------------------------------
# Descriptions of stage settings (catalyst spec options and settings of the app), shown when hovering their rows in
# stage settings. Keys are names of options in spec files. Options of targets not listed here (eg. diskimage/use,
# embedded/packages) use description of the same option of other targets (_TARGET_OPTION_DESCRIPTIONS).

STAGE_ARGUMENT_DESCRIPTIONS: dict[str, str] = {
    # Settings of the app:
    "name": "Name of stage in project, also name of its folder.",
    "parent": "Stage whose build is used as seed of this stage. Stage without parent uses downloaded seed, eg. stage3 "
              "of Gentoo releases.",
    "releng_template": "Spec of Gentoo Releng that this stage is based on. Settings using \"Inherit from Releng "
                       "template\" take values from it.",
    # Basic:
    "target": "Catalyst target built by this stage: stage1, stage3, stage4, livecd-stage1, livecd-stage2...",
    "profile": "Portage profile used to build stage and set in the result, eg. default/linux/arm64/23.0.",
    "subarch": "Subarchitecture from catalyst architecture files (eg. amd64, arm64, cell). It sets default CHOST and "
               "compiler flags of stage.",
    "version_stamp": "Version in name of built stage (target-subarch-version_stamp), eg. @TIMESTAMP@ (time of build).",
    "rel_type": "Release type: folder of builds where stage is stored, eg. 23.0-default. Stages using each other as "
                "seeds are usually built with the same type.",
    "compression_mode": "How stage archive is compressed, eg. pixz (parallel xz), xz, zstd or gzip.",
    "snapshot_treeish": "Commit of Gentoo repository snapshot used to build stage. Set by snapshot selected for build.",
    "source_subpath": "Seed of stage, archive relative to catalyst builds folder. Set by the app from parent stage.",
    # Architecture and compilation:
    "chost": "System type that stage is built for (CHOST), eg. aarch64-unknown-linux-gnu. Normally set by subarch, "
             "changing it in stage1 builds toolchain for other system type.",
    "cbuild": "System type of machine building stage (CBUILD), set when cross compiling. Normally not changed.",
    "common_flags": "COMMON_FLAGS of make.conf: compiler flags used for C, C++ and Fortran unless set separately, "
                    "eg. -O2 -pipe -mcpu=cortex-a76.",
    "cflags": "CFLAGS of make.conf: flags of C compiler. Default is common flags of subarch. Optimizations make stage "
              "less generic.",
    "cxxflags": "CXXFLAGS of make.conf: flags of C++ compiler. Same as CFLAGS by default.",
    "fcflags": "FCFLAGS of make.conf: flags of Fortran compiler (modern Fortran).",
    "fflags": "FFLAGS of make.conf: flags of Fortran 77 compiler.",
    "asflags": "ASFLAGS of make.conf: flags of assembler.",
    "ldflags": "LDFLAGS of make.conf: flags of linker, eg. -Wl,-O1 -Wl,--as-needed.",
    "interpreter": "QEMU user mode emulators copied into stage, so programs of other architecture can run when "
                   "building on this machine.",
    # Packages and repositories:
    "repos": "Overlays (ebuild repositories) available while building stage, in addition to Gentoo repository.",
    "keep_repos": "Overlays stay configured in built stage (repos.conf), so they can be used in it later.",
    "binrepo_path": "Path of binary packages host written to binrepos.conf of stage, eg. arm64/binpackages/23.0/arm64. "
                    "Used by systems installed from stage to download prebuilt packages.",
    "pkgcache_path": "Folder keeping binary packages built by stage. Later builds reuse them instead of compiling "
                     "packages again.",
    "kerncache_path": "Folder keeping kernels built by stage. Later builds reuse them when kernel sources and "
                      "configuration didn't change.",
    "hostuse": "USE flags of build host added to USE in make.conf of stage.",
    "catalyst_use": "USE flags used only while building stage, not kept in make.conf of built stage, eg. lto or "
                    "bindist.",
    "install_mask": "Files and folders that Portage doesn't install (INSTALL_MASK), eg. /usr/share/doc.",
    "portage_confdir": "Portage configuration (package.use, package.accept_keywords, env...) copied to /etc/portage "
                       "while building stage. Sources selected here are combined.",
    "portage_prefix": "Name of files added to Portage configuration from portage confdir (eg. package.use/<name>). "
                      "They are removed from built stage.",
    "decompressor_search_order": "Order in which tools are tried when unpacking seed and caches, eg. lbzip2 bzip2 tar "
                                 "pixz xz gzip squashfs.",
    "update_seed": "Seed is updated (packages needed by compiler) before building stage1, so old seed can build "
                   "with newer Gentoo repository.",
    "update_seed_command": "Emerge arguments used to update seed when Update seed is enabled, eg. --update "
                           "dev-libs/mpfr dev-libs/mpc dev-libs/gmp. By default dependencies of gcc are updated.",
    "rename_regexp": "Sed expression renaming built stage archive, eg. s/-@TIMESTAMP@//.",
    "boot/kernel": "Kernels built for stage, with their settings.",
    # Stage4:
    "stage4/packages": "Packages installed in stage4, built with its USE flags.",
    "stage4/use": "USE flags of packages installed in stage4, added to USE flags of profile.",
    "stage4/fsscript": "Shell script run inside stage after its packages and kernels are installed, eg. to configure "
                       "system.",
    "stage4/rcadd": "Services added to runlevels, as service|runlevel, eg. sshd|default.",
    "stage4/rcdel": "Services removed from runlevels, as service|runlevel, or only service to remove it from all "
                    "runlevels.",
    "stage4/users": "Users created in stage. They are added to groups users, wheel, audio, games, cdrom and usb.",
    "stage4/groups": "Groups created in stage.",
    "stage4/ssh_public_keys": "SSH public keys allowed to log in as root (authorized_keys of root).",
    "stage4/root_overlay": "Files copied into stage filesystem, eg. configuration files. Sources selected here are "
                           "combined.",
    "stage4/unmerge": "Packages removed after kernels are built, eg. build tools not needed in result.",
    "stage4/empty": "Folders emptied at the end of build, eg. /var/tmp /var/cache.",
    "stage4/rm": "Files removed at the end of build, wildcards can be used, eg. /usr/lib/*.a.",
    "stage4/gk_mainargs": "Arguments of genkernel for all kernels of stage, eg. --lvm --mdadm.",
    "stage4/linuxrc": "Own linuxrc script used by genkernel in initramfs instead of its default one.",
    # Live CD:
    "livecd/packages": "Packages installed in live system.",
    "livecd/use": "USE flags of packages installed in live system, added to USE flags of profile.",
    "livecd/type": "Kind of ISO, which sets defaults of catalyst (motd, services, files): gentoo-release-minimal "
                   "(minimal installation CD), gentoo-release-livecd (LiveGUI) or generic-livecd (other media).",
    "livecd/iso": "File name of created ISO image.",
    "livecd/volid": "Volume ID (label) of ISO image.",
    "livecd/fstype": "Filesystem of live system image in ISO. The only possible value is squashfs.",
    "livecd/fsops": "Arguments of tool creating filesystem of live system (squashfs).",
    "livecd/fsscript": "Shell script run inside live system after its packages and kernels are installed.",
    "livecd/bootargs": "Arguments of kernel added to boot menu of ISO, eg. dokeymap (asks for keyboard layout).",
    "livecd/cdtar": "Archive with bootloader files and configuration of ISO. Needed on architectures where catalyst "
                    "doesn't generate it.",
    "livecd/gk_mainargs": "Arguments of genkernel for all kernels of live system, eg. --lvm --mdadm.",
    "livecd/linuxrc": "Own linuxrc script used by genkernel in initramfs instead of its default one.",
    "livecd/depclean": "Removing packages not needed anymore after fsscript: empty removes build dependencies too "
                       "(smallest ISO), keepbdeps keeps them, no doesn't remove anything.",
    "livecd/rcadd": "Services added to runlevels, as service|runlevel, eg. sshd|default.",
    "livecd/rcdel": "Services removed from runlevels, as service|runlevel, or only service to remove it from all "
                    "runlevels.",
    "livecd/modblacklist": "Kernel modules that aren't loaded automatically, eg. modules causing problems on some "
                           "hardware.",
    "livecd/unmerge": "Packages removed after kernels are built, eg. compiler and build tools not needed in live "
                      "system.",
    "livecd/empty": "Folders emptied at the end of build, eg. /var/tmp /usr/share/doc.",
    "livecd/rm": "Files removed at the end of build, wildcards can be used, eg. /usr/lib/*.a.",
    "livecd/users": "Users created in live system. They are added to groups users, wheel, audio, games, cdrom and usb.",
    "livecd/motd": "Message shown after logging in. Gentoo types of ISO have default message.",
    "livecd/readme": "README.txt in root of ISO. Gentoo types of ISO have default file.",
    "livecd/root_overlay": "Files copied into filesystem of live system. Sources selected here are combined.",
    "livecd/overlay": "Files copied into ISO next to live system image (not into live system), eg. documentation. "
                      "Sources selected here are combined.",
    "livecd/verify": "Checksums of ISO contents are created, so its files can be verified when booting.",
    "livecd/iso_extra_partition": "Size (in kilobytes, at least 300000) of extra XFS partition for persistent storage, "
                                  "added to ISO used as USB disk image.",
    # Disk image:
    "diskimage/type": "Kind of disk image, which sets defaults of catalyst, eg. console or cloud-init.",
    "diskimage/qcow2": "File name of created qcow2 disk image.",
    "diskimage/qcow2_size": "Size of qcow2 disk image in GiB (default 20).",
    "diskimage/qcow2_efisize": "Size of EFI partition of disk image in GiB.",
    "diskimage/qcow2_roottype": "Filesystem of root partition of disk image (default xfs).",
    "diskimage/sshkey": "SSH public key allowed to log in as root in disk image.",
    "diskimage/volid": "Volume ID (label) of disk image.",
    "diskimage/verify": "Checksums of image contents are created, so its files can be verified when booting.",
    "diskimage/fsops": "Arguments of tool creating filesystem of disk image.",
    "diskimage/depclean": "Removing packages not needed anymore after fsscript: empty removes build dependencies too "
                          "(smallest image), keepbdeps keeps them, no doesn't remove anything.",
    # Embedded:
    "embedded/mergeroot": "Packages are installed into separate root (/tmp/mergeroot) with only their runtime files.",
    "embedded/fs-type": "Filesystem of created image, eg. squashfs or jffs2.",
    "embedded/fs-ops": "Arguments of tool creating filesystem of image.",
    "embedded/fs-prepare": "Script run before filesystem image is created.",
    "embedded/fs-finish": "Script run after filesystem image is created.",
    "embedded/runscript": "Own script of catalyst run instead of default steps of target.",
    # Netboot:
    "netboot/busybox_config": "Busybox configuration file, busybox makes root filesystem of netboot image.",
    "netboot/overlay": "Files copied into netboot image outside of its root filesystem.",
    "netboot/extra_files": "Files not belonging to packages copied into netboot image, eg. /lib/libresolv.so.2.",
}

# Options available in multiple targets (<target>/<option>), for targets without own description.
_TARGET_OPTION_DESCRIPTIONS: dict[str, str] = {
    option.split("/", 1)[1]: description
    for option, description in STAGE_ARGUMENT_DESCRIPTIONS.items()
    if option.startswith("livecd/")
    or option in {"stage4/groups", "stage4/ssh_public_keys"}
}

def stage_argument_description(name: str) -> str | None:
    """Description of stage setting by its name in spec, None for unknown ones."""
    if description := STAGE_ARGUMENT_DESCRIPTIONS.get(name):
        return description
    if "/" in name:
        target, option = name.split("/", 1)
        if target != "livecd" and (description := _TARGET_OPTION_DESCRIPTIONS.get(option)):
            # Text of live CD option, for other targets.
            return description.replace("live system", "image").replace("ISO", "image").replace(" Sources selected here are combined.", "")
    return None
