from __future__ import annotations
import os, re, subprocess
from dataclasses import dataclass, field
from enum import Enum
from .deploy_system import NetworkService, NETWORK_SERVICE_FILES, WPA_SUPPLICANT_FILE

# ------------------------------------------------------------------------------
# Booting deployed system: bootloaders found in stage archive, and installing them (with packages from Gentoo
# repository when stage doesn't contain them). Root filesystem is mounted at MOUNT_POINT on machine, ESP in it.

_X86 = ("x86_64", "i686", "i586", "i486")

class Bootloader(Enum):
    GRUB = "grub"
    SYSTEMD_BOOT = "systemd-boot"
    REFIND = "refind"
    EFI_STUB = "efi-stub"
    KBOOT = "kboot" # Configuration of petitboot (PS3), no bootloader is installed.
    RASPBERRY_PI = "raspberry-pi" # Firmware of Raspberry Pi boots kernel from FAT /boot (config.txt, cmdline.txt).
    NONE = "none"

    @property
    def display_name(self) -> str:
        return {
            Bootloader.GRUB: "GRUB",
            Bootloader.SYSTEMD_BOOT: "systemd-boot",
            Bootloader.REFIND: "rEFInd",
            Bootloader.EFI_STUB: "EFI stub (kernel started by firmware)",
            Bootloader.KBOOT: "kboot (PS3 petitboot)",
            Bootloader.RASPBERRY_PI: "Raspberry Pi firmware",
            Bootloader.NONE: "Don't install bootloader",
        }[self]

    def package(self, init: str | None) -> str | None:
        """Package providing bootloader."""
        return {
            Bootloader.GRUB: "sys-boot/grub",
            Bootloader.SYSTEMD_BOOT: "sys-apps/systemd" if init == "systemd" else "sys-apps/systemd-utils",
            Bootloader.REFIND: "sys-boot/refind",
            Bootloader.EFI_STUB: "sys-boot/efibootmgr",
            # Boot files of older models (Raspberry Pi 5 has them in EEPROM).
            Bootloader.RASPBERRY_PI: "sys-boot/raspberrypi-firmware",
        }.get(self)

    @property
    def configuration_files(self) -> list[str]:
        """Files in /boot that stage can have and package of bootloader refuses to install over (when no package owns
        them): Raspberry Pi firmware package with config.txt of stages made for Raspberry Pi. They are kept aside while
        package is installed and put back after, configuration of stage is used."""
        return {Bootloader.RASPBERRY_PI: ["config.txt", "cmdline.txt"]}.get(self, [])

    @property
    def needs_package(self) -> bool:
        """Configuration only bootloaders don't need anything installed in stage."""
        return self not in (Bootloader.KBOOT, Bootloader.NONE)

    def supported(self, architecture: str, uefi: bool, removable: bool = False) -> bool:
        """Removable disks (prepared on this computer) boot from fallback path of ESP, without boot entries in firmware,
        which EFI stub needs."""
        if self == Bootloader.NONE:
            return True
        if self == Bootloader.EFI_STUB and removable:
            return False
        if self == Bootloader.KBOOT:
            return architecture in ("ppc64", "ppc64le") and not uefi # Machines booting with petitboot.
        if self == Bootloader.RASPBERRY_PI:
            from .deploy_target import RASPBERRY_PI_ARCHITECTURES
            return architecture in RASPBERRY_PI_ARCHITECTURES and not uefi
        if self == Bootloader.GRUB:
            return uefi or architecture in _X86
        return uefi # Others are UEFI applications.

@dataclass
class BootloaderWarning:
    """Something bootloader installation can't set up or keep updated automatically."""
    summary: str
    details: str

def bootloader_warnings(bootloader: Bootloader, uefi: bool, removable: bool, has_kernel: bool) -> list[BootloaderWarning]:
    """What can't be set up for booting installed system. Removable disks (prepared on this computer) get no boot entry
    in firmware of target machine."""
    warnings = []
    if bootloader != Bootloader.NONE and not has_kernel:
        failure = ("its menu will be empty" if bootloader == Bootloader.GRUB
                   else "installation fails at Install bootloader step, as it can't be configured without kernel")
        warnings.append(BootloaderWarning(
            "No kernel to boot",
            f"Stage doesn't contain kernel and no kernel is installed, so system can't boot. {bootloader.display_name} "
            f"is installed, but {failure}. Select distribution kernel to install one."))
    if removable and uefi and bootloader in (Bootloader.GRUB, Bootloader.SYSTEMD_BOOT, Bootloader.REFIND):
        warnings.append(BootloaderWarning(
            "No firmware boot entry",
            "No boot entry is added to firmware of the machine, it starts the disk from fallback path EFI/BOOT. "
            "If it doesn't, select the disk in boot menu of the machine."))
    return warnings

class Kernel(Enum):
    STAGE = "stage"
    DISTRIBUTION_BINARY = "gentoo-kernel-bin"
    DISTRIBUTION = "gentoo-kernel"
    RASPBERRY_PI = "raspberrypi-image" # Prebuilt kernel of Raspberry Pi, with device trees and overlays.
    NONE = "none"

    @property
    def display_name(self) -> str:
        return {
            Kernel.STAGE: "Kernel from stage",
            Kernel.DISTRIBUTION_BINARY: "Distribution kernel, prebuilt",
            Kernel.DISTRIBUTION: "Distribution kernel, compiled on machine",
            Kernel.RASPBERRY_PI: "Raspberry Pi kernel, prebuilt",
            Kernel.NONE: "Don't install kernel",
        }[self]

    @property
    def package(self) -> str | None:
        return {Kernel.DISTRIBUTION_BINARY: "sys-kernel/gentoo-kernel-bin", Kernel.DISTRIBUTION: "sys-kernel/gentoo-kernel",
                Kernel.RASPBERRY_PI: "sys-kernel/raspberrypi-image"}.get(self)

    def supported(self, architecture: str) -> bool:
        if self == Kernel.RASPBERRY_PI:
            from .deploy_target import RASPBERRY_PI_ARCHITECTURES
            return architecture in RASPBERRY_PI_ARCHITECTURES
        return True

FIRMWARE_PACKAGE = "sys-kernel/linux-firmware"

# Files showing that stage contains bootloader (paths in archive, without leading ./).
_BOOTLOADER_FILES = {
    Bootloader.GRUB: re.compile(r"^usr/s?bin/grub-install$"),
    Bootloader.SYSTEMD_BOOT: re.compile(r"^usr/lib/systemd/boot/efi/systemd-boot\w+\.efi$"),
    Bootloader.REFIND: re.compile(r"^usr/s?bin/refind-install$"),
    Bootloader.EFI_STUB: re.compile(r"^usr/s?bin/efibootmgr$"),
    # Firmware files, or device trees of Raspberry Pi 5 (its firmware is in EEPROM, it needs only kernel and them).
    Bootloader.RASPBERRY_PI: re.compile(r"^boot/(start4?\.elf|bootcode\.bin|bcm2712[^/]*\.dtb)$"),
}
_KERNEL = re.compile(r"^boot/(vmlinuz|vmlinux|kernel|Image)[^/]*$")
_INITRAMFS = re.compile(r"^boot/(initramfs|initrd)[^/]*$")
_FIRMWARE = re.compile(r"^var/db/pkg/sys-kernel/linux-firmware-[^/]+/")
# Firmware packages that block linux-firmware (they install some of its files).
# Line of tar -tv listing: permissions, owner, size, date, time and path (symbolic link with its target).
_LISTING_LINE = re.compile(r"^\S+\s+\S+\s+\d+\s+\S+\s+\S+\s+(.*?)(?: -> (.*))?$")
_BSD_LISTING_LINE = re.compile(r"^\S+\s+\d+\s+\S+\s+\S+\s+\d+\s+\S+\s+\d+\s+\S+\s+(.*?)(?: -> (.*))?$")
# Groups of system: base ones of sys-apps/baselayout, others are created by acct-group packages.
BASELAYOUT_GROUPS = frozenset("root bin daemon sys adm tty disk lp mem kmem wheel floppy news uucp console audio cdrom dialout "
                              "tape video cdrw usb users portage utmp nogroup".split())
_ACCOUNT_GROUP = re.compile(r"^var/db/pkg/acct-group/(.+?)-\d[^/]*/$")
_FIRMWARE_CONFLICTS = re.compile(r"^var/db/pkg/(sys-firmware/raspberrypi-wifi-ucode)-[^/]+/")

@dataclass
class StageContents:
    """What stage archive contains, for booting it."""
    bootloaders: set[Bootloader] = field(default_factory=set)
    kernels: list[str] = field(default_factory=list) # Files in /boot.
    initramfs: list[str] = field(default_factory=list)
    init: str | None = None # openrc or systemd.
    _init_from_link: bool = field(default=False, init=False, repr=False)
    network_services: set[NetworkService] = field(default_factory=set)
    firmware: bool = False # linux-firmware is installed.
    wpa_supplicant: bool = False # Wi-Fi of network services other than NetworkManager.
    firmware_conflict: str | None = None # Installed package that can't be installed together with linux-firmware.
    account_groups: set[str] = field(default_factory=set) # Groups created by acct-group packages.

    @property
    def groups(self) -> set[str]:
        """Groups existing in stage."""
        return set(BASELAYOUT_GROUPS) | self.account_groups

    @classmethod
    def scan(cls, archive_path: str, process_holder: list | None = None) -> StageContents:
        """Reads listing of archive made by catalyst (CONTENTS.gz next to it, tar -tv format), or lists archive (tar of
        this computer handles all compressions used by catalyst, it decompresses whole archive)."""
        contents = cls()
        listing = archive_path + ".CONTENTS.gz"
        if os.path.isfile(listing):
            import gzip
            with gzip.open(listing, "rt", encoding="utf-8", errors="replace") as file:
                for line in file:
                    if match := _LISTING_LINE.match(line.rstrip("\n")):
                        contents._add_path(match.group(1).removeprefix("./"), match.group(2))
            if contents.init or contents.kernels or contents.bootloaders:
                return contents
            contents = cls() # Listing in other format, archive is listed.
        process = subprocess.Popen(["tar", "-tvf", archive_path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, errors="replace", env={**os.environ, "LC_ALL": "C"})
        if process_holder is not None:
            process_holder.append(process)
        with process.stdout as output:
            for line in output:
                match = _BSD_LISTING_LINE.match(line.rstrip("\n")) or _LISTING_LINE.match(line.rstrip("\n"))
                if match:
                    contents._add_path(match.group(1).removeprefix("./"), match.group(2))
        if process.wait() != 0:
            raise RuntimeError("Failed to list stage archive")
        return contents

    def _add_path(self, path: str, link_target: str | None = None):
        """Records what path of archive shows (bootloader, services, kernel, init system...)."""
        contents = self
        if path in ("sbin/init", "bin/init", "usr/sbin/init", "usr/bin/init") and link_target:
            target = link_target.rsplit("/", 1)[-1]
            if target in ("systemd", "openrc-init", "sysvinit", "busybox"):
                contents.init = "systemd" if target == "systemd" else "openrc"
                contents._init_from_link = True
        for bootloader, pattern in _BOOTLOADER_FILES.items():
            if pattern.match(path):
                contents.bootloaders.add(bootloader)
        for service, pattern in NETWORK_SERVICE_FILES.items():
            if pattern.match(path):
                contents.network_services.add(service)
        if not contents.firmware and _FIRMWARE.match(path):
            contents.firmware = True
        if not contents.wpa_supplicant and WPA_SUPPLICANT_FILE.match(path):
            contents.wpa_supplicant = True
        if path.startswith("var/db/pkg/acct-group/") and (match := _ACCOUNT_GROUP.match(path)):
            contents.account_groups.add(match.group(1))
        if contents.firmware_conflict is None and (match := _FIRMWARE_CONFLICTS.match(path)):
            contents.firmware_conflict = match.group(1)
        if _KERNEL.match(path):
            contents.kernels.append(path.removeprefix("boot/"))
        elif _INITRAMFS.match(path):
            contents.initramfs.append(path.removeprefix("boot/"))
        elif path in ("sbin/openrc-run", "usr/sbin/openrc-run", "usr/bin/openrc-run") and contents.init is None:
            contents.init = "openrc"
        elif path in ("usr/lib/systemd/systemd", "lib/systemd/systemd") and not contents._init_from_link:
            contents.init = "systemd"

    @property
    def summary(self) -> str:
        bootloaders = ", ".join(item.display_name.split(" (")[0] for item in Bootloader if item in self.bootloaders) or "no bootloader"
        kernel = f"kernel {', '.join(self.kernels)}" if self.kernels else "no kernel"
        firmware = ", linux-firmware" if self.firmware else ""
        return f"Stage contains {bootloaders}, {kernel}{firmware}" + (f", {self.init}" if self.init else "")

def default_bootloader(contents: StageContents, architecture: str, uefi: bool, removable: bool = False,
                       raspberry_pi: bool = False) -> Bootloader:
    """First supported bootloader found in stage, otherwise GRUB (installed from repository) when supported."""
    if raspberry_pi:
        return Bootloader.RASPBERRY_PI
    if Bootloader.KBOOT.supported(architecture, uefi, removable):
        return Bootloader.KBOOT
    for bootloader in (Bootloader.GRUB, Bootloader.SYSTEMD_BOOT, Bootloader.REFIND, Bootloader.EFI_STUB):
        if bootloader in contents.bootloaders and bootloader.supported(architecture, uefi, removable):
            return bootloader
    return Bootloader.GRUB if Bootloader.GRUB.supported(architecture, uefi, removable) else Bootloader.NONE

# ------------------------------------------------------------------------------
# Scripts run on machine (bash, ROOT is root of installed system).

# Prepares chroot of installed system, it's cleaned when script ends.
CHROOT_SETUP = r'''
cp -L /etc/resolv.conf "$ROOT/etc/resolv.conf"
mountpoint -q "$ROOT/proc" || mount --types proc /proc "$ROOT/proc"
mountpoint -q "$ROOT/sys" || { mount --rbind /sys "$ROOT/sys" && mount --make-rslave "$ROOT/sys"; }
mountpoint -q "$ROOT/dev" || { mount --rbind /dev "$ROOT/dev" && mount --make-rslave "$ROOT/dev"; }
mountpoint -q "$ROOT/run" || { mount --bind /run "$ROOT/run" && mount --make-slave "$ROOT/run"; }
trap 'umount -l "$ROOT/run" "$ROOT/dev" "$ROOT/sys" "$ROOT/proc" 2>/dev/null' EXIT
in_root() { chroot "$ROOT" /usr/bin/env -i HOME=/root TERM=dumb PATH=/usr/sbin:/usr/bin:/sbin:/bin "$@"; }
'''

def packages_script(packages: list[str], grub_platform: str | None, init: str | None, generic_initramfs: bool = False,
                    bootloader: Bootloader | None = None) -> str:
    """Installs packages in installed system with emerge. Gentoo repository is downloaded when stage doesn't have it.
    Binary packages are used when system has binary repository configured. Generic initramfs (with all drivers) is
    generated when system is installed on other computer than the one it boots on."""
    lines = ["set -e", CHROOT_SETUP]
    lines.append(r'''
if [ ! -e "$ROOT/var/db/repos/gentoo/profiles" ]; then
    echo "Downloading Gentoo repository"
    in_root emerge-webrsync -q
fi
''')
    if grub_platform:
        lines.append(f'grep -q "^GRUB_PLATFORMS=" "$ROOT/etc/portage/make.conf" || echo \'GRUB_PLATFORMS="{grub_platform}"\' >> "$ROOT/etc/portage/make.conf"')
    # Portage configuration can be file or directory (eg. package.license), entry is added to file or as file.
    lines.append('''portage_entry() {
    if [ -f "$ROOT/etc/portage/$1" ]; then echo "$3" >> "$ROOT/etc/portage/$1"
    else mkdir -p "$ROOT/etc/portage/$1" && echo "$3" > "$ROOT/etc/portage/$1/$2"; fi
}''')
    if any(package in ("sys-apps/systemd", "sys-apps/systemd-utils") for package in packages):
        package = "sys-apps/systemd" if init == "systemd" else "sys-apps/systemd-utils"
        lines.append(f'portage_entry package.use catalystlab-boot "{package} boot kernel-install"')
    if any(package.startswith("sys-kernel/gentoo-kernel") for package in packages):
        # Distribution kernel is installed to /boot by installkernel, with initramfs generated by dracut. With GRUB,
        # installkernel also regenerates its menu, so kernels installed later are added to it.
        flags = "dracut grub" if bootloader == Bootloader.GRUB else "dracut"
        lines.append(f'portage_entry package.use catalystlab-kernel "sys-kernel/installkernel {flags}"')
        if generic_initramfs:
            lines.append('mkdir -p "$ROOT/etc/dracut.conf.d" && echo \'hostonly="no"\' > "$ROOT/etc/dracut.conf.d/catalystlab.conf"')
    if FIRMWARE_PACKAGE in packages:
        lines.append(f'portage_entry package.license catalystlab-firmware "{FIRMWARE_PACKAGE} linux-fw-redistributable"')
    # Configuration of stage that package of bootloader would refuse to install over is kept aside while it's
    # installed, and put back after (also when installation fails).
    kept_files = bootloader.configuration_files if bootloader and bootloader.package(init) in packages else []
    if kept_files:
        lines.append(f'''
KEPT_BOOT_FILES=""
for FILE in {" ".join(kept_files)}; do
    if [ -f "$ROOT/boot/$FILE" ]; then
        mv "$ROOT/boot/$FILE" "$ROOT/boot/$FILE.catalystlab-stage"
        KEPT_BOOT_FILES="$KEPT_BOOT_FILES $FILE"
    fi
done
restore_boot_files() {{
    for FILE in $KEPT_BOOT_FILES; do
        mv -f "$ROOT/boot/$FILE.catalystlab-stage" "$ROOT/boot/$FILE"
    done
    KEPT_BOOT_FILES=""
}}
# Replaces trap of chroot setup, mounts are released too.
trap 'restore_boot_files; umount -l "$ROOT/run" "$ROOT/dev" "$ROOT/sys" "$ROOT/proc" 2>/dev/null' EXIT
''')
    binpkgs = '$( [ -n "$(ls -A "$ROOT/etc/portage/binrepos.conf" 2>/dev/null)" ] && echo --getbinpkg )'
    atoms = " ".join(packages)
    lines.append(f'echo "Installing {atoms}"')
    lines.append(f'in_root emerge --noreplace --quiet-build=y {binpkgs} {atoms}')
    if kept_files:
        lines.append('restore_boot_files')
    return "\n".join(lines) + "\n"

def bootloader_script(bootloader: Bootloader, architecture: str, uefi: bool, disk: str, root_device: str,
                      efi_device: str | None, esp: str | None, separate_boot: bool = False, removable: bool = False,
                      ps3: bool | None = None) -> str | None:
    """Installs and configures bootloader in installed system. ESP is mount point of EFI system partition. Removable
    disks (prepared on this computer for other machine) get bootloader in fallback path of ESP, without boot entries in
    firmware of this computer."""
    if bootloader == Bootloader.KBOOT:
        return kboot_script(root_device, separate_boot, ps3=ps3, by_partuuid=removable)
    if bootloader == Bootloader.RASPBERRY_PI:
        return raspberry_pi_script(root_device, architecture)
    if uefi and not esp:
        return None
    root = f'root=UUID=$(blkid -s UUID -o value {root_device}) rw'
    kernel = r'''
KERNEL=$(cd "$ROOT/boot" && ls -t vmlinuz* kernel* Image* vmlinux* 2>/dev/null | head -n 1)
INITRD=$(cd "$ROOT/boot" && ls -t initramfs* initrd* 2>/dev/null | head -n 1)
[ -n "$KERNEL" ] || { echo "No kernel in /boot, bootloader can't be configured"; exit 1; }
echo "Kernel: $KERNEL${INITRD:+, initramfs: $INITRD}"
'''
    match bootloader:
        case Bootloader.GRUB:
            target = grub_target(architecture, uefi)
            if target is None:
                return None
            install = f"grub-install --target={target} --efi-directory={esp} --bootloader-id=Gentoo" if uefi else f"grub-install --target={target} {disk}"
            if uefi and removable:
                install += " --removable --no-nvram"
            return f"""set -e
{CHROOT_SETUP}
in_root {install}
in_root grub-mkconfig -o /boot/grub/grub.cfg
"""
        case Bootloader.SYSTEMD_BOOT:
            # Kernel and initramfs are copied to ESP, where systemd-boot reads them.
            return f"""set -e
{CHROOT_SETUP}
{kernel}
in_root bootctl install --esp-path={esp}{' --no-variables' if removable else ''}
mkdir -p "$ROOT{esp}/gentoo" "$ROOT{esp}/loader/entries"
cp "$ROOT/boot/$KERNEL" "$ROOT{esp}/gentoo/linux"
[ -z "$INITRD" ] || cp "$ROOT/boot/$INITRD" "$ROOT{esp}/gentoo/initrd"
{{
    echo "title Gentoo Linux"
    echo "linux /gentoo/linux"
    [ -z "$INITRD" ] || echo "initrd /gentoo/initrd"
    echo "options {root}"
}} > "$ROOT{esp}/loader/entries/gentoo.conf"
printf 'default gentoo.conf\\ntimeout 3\\n' > "$ROOT{esp}/loader/loader.conf"
cat "$ROOT{esp}/loader/entries/gentoo.conf"
"""
        case Bootloader.REFIND:
            # rEFInd finds kernels in /boot, with options from refind_linux.conf. Its installer looks for ESP in
            # /boot/efi, it's bound there when mounted elsewhere.
            bind = esp != "/boot/efi"
            return f"""set -e
{CHROOT_SETUP}
{kernel}
{f'mkdir -p "$ROOT/boot/efi" && mount --bind "$ROOT{esp}" "$ROOT/boot/efi"' if bind else ''}
in_root refind-install --yes{f' --usedefault {efi_device}' if removable and efi_device else ''} || {{ {'umount "$ROOT/boot/efi";' if bind else ''} exit 1; }}
{'umount "$ROOT/boot/efi"' if bind else ''}
echo "\\"Boot with defaults\\" \\"{root}\\"" > "$ROOT/boot/refind_linux.conf"
cat "$ROOT/boot/refind_linux.conf"
"""
        case Bootloader.EFI_STUB:
            # Firmware starts kernel (built with EFI stub) directly from ESP.
            partition = re.search(r"(\d+)$", efi_device or "")
            return f"""set -e
{CHROOT_SETUP}
{kernel}
mkdir -p "$ROOT{esp}/EFI/gentoo"
cp "$ROOT/boot/$KERNEL" "$ROOT{esp}/EFI/gentoo/linux.efi"
OPTIONS="{root}"
if [ -n "$INITRD" ]; then cp "$ROOT/boot/$INITRD" "$ROOT{esp}/EFI/gentoo/initrd"; OPTIONS="$OPTIONS initrd=\\\\EFI\\\\gentoo\\\\initrd"; fi
in_root efibootmgr --create --disk {disk} --part {partition.group(1) if partition else 1} --label Gentoo --loader '\\EFI\\gentoo\\linux.efi' --unicode "$OPTIONS"
"""
    return None

def kboot_script(root_device: str, separate_boot: bool, ps3: bool | None = None, by_partuuid: bool = False) -> str:
    """Entries of petitboot (kboot.conf) for kernels in /boot, newest first (first entry is default). With separate
    /boot partition configuration is in it and paths are relative to it, otherwise it's in /etc. Like PS3 Gentoo
    installer (github.com/damiandudycz/ps3). PS3 is detected on machine when not given. Disks prepared for other
    machine reference root partition by PARTUUID (kernel finds it without initramfs), device names differ there."""
    config = "$ROOT/boot/kboot.conf" if separate_boot else "$ROOT/etc/kboot.conf"
    prefix = "" if separate_boot else "/boot"
    root = f"PARTUUID=$(blkid -s PARTUUID -o value {root_device})" if by_partuuid else root_device
    detect_ps3 = {True: "true", False: "false"}.get(ps3, 'grep -qi "PS3" /proc/device-tree/model 2>/dev/null')
    return f"""set -e
CONFIG="{config}"
OPTIONS=""
ROOT_DEVICE="{root}"
[ "$ROOT_DEVICE" != "PARTUUID=" ] || {{ echo "Failed to read PARTUUID of {root_device}"; exit 1; }}
# PS3 framebuffer mode used by PS3 Gentoo images.
{detect_ps3} && OPTIONS=" video=ps3fb:mode:133"
KERNELS=$(cd "$ROOT/boot" && ls -t vmlinux* vmlinuz* 2>/dev/null || true)
[ -n "$KERNELS" ] || {{ echo "No kernel in /boot, kboot entries can't be created"; exit 1; }}
: > "$CONFIG.new"
for KERNEL in $KERNELS; do
    VERSION=${{KERNEL#vmlinu?-}}
    INITRD=$(cd "$ROOT/boot" && ls -t "initramfs-$VERSION"* "initrd-$VERSION"* 2>/dev/null | head -n 1 || true)
    ENTRY="Gentoo-$VERSION='{prefix}/$KERNEL${{INITRD:+ initrd={prefix}/$INITRD}} root=$ROOT_DEVICE$OPTIONS'"
    echo "$ENTRY" >> "$CONFIG.new"
done
# Entries added manually before are kept after new ones.
[ -f "$CONFIG" ] && grep -vxF -f "$CONFIG.new" "$CONFIG" >> "$CONFIG.new" || true
mv "$CONFIG.new" "$CONFIG"
echo "kboot configuration ${{CONFIG#$ROOT}}:"
cat "$CONFIG"
"""

def raspberry_pi_script(root_device: str, architecture: str) -> str:
    """Boot configuration read by Raspberry Pi firmware from FAT /boot: cmdline.txt with root partition (by PARTUUID,
    device names differ between SD card, USB and NVMe), and config.txt when stage doesn't have one. Firmware picks
    kernel for model itself (kernel_2712.img on Pi 5, kernel8.img on other 64-bit models)."""
    arm_64bit = "arm_64bit=1" if architecture == "aarch64" else ""
    return f"""set -e
ls "$ROOT"/boot/kernel*.img > /dev/null 2>&1 || {{ echo "No Raspberry Pi kernel in /boot (kernel*.img), boot configuration can't be created"; exit 1; }}
echo "Kernels: $(cd "$ROOT/boot" && ls kernel*.img | tr '\\n' ' ')"
PARTUUID=$(blkid -s PARTUUID -o value {root_device})
[ -n "$PARTUUID" ] || {{ echo "Failed to read PARTUUID of {root_device}"; exit 1; }}
FSTYPE=$(blkid -s TYPE -o value {root_device})
echo "console=serial0,115200 console=tty1 root=PARTUUID=$PARTUUID rootfstype=$FSTYPE fsck.repair=yes rootwait" > "$ROOT/boot/cmdline.txt"
if [ ! -f "$ROOT/boot/config.txt" ]; then
    printf '%s\\n' "# Created by Catalyst Lab, see https://www.raspberrypi.com/documentation/computers/config_txt.html" {arm_64bit and f'"{arm_64bit}"'} 'dtparam=audio=on' 'dtoverlay=vc4-kms-v3d' 'max_framebuffers=2' 'auto_initramfs=1' > "$ROOT/boot/config.txt"
fi
echo "cmdline.txt:"; cat "$ROOT/boot/cmdline.txt"
echo "config.txt:"; cat "$ROOT/boot/config.txt"
"""

def grub_target(architecture: str, uefi: bool) -> str | None:
    """GRUB platform for machine architecture."""
    if not uefi:
        return "i386-pc" if architecture in _X86 else None
    return {"x86_64": "x86_64-efi", "i686": "i386-efi", "aarch64": "arm64-efi", "riscv64": "riscv64-efi",
            "armv7l": "arm-efi"}.get(architecture)

def grub_platform(architecture: str, uefi: bool) -> str | None:
    """GRUB_PLATFORMS value for building GRUB."""
    if not uefi:
        return "pc" if architecture in _X86 else None
    return "efi-32" if architecture == "i686" else "efi-64"
