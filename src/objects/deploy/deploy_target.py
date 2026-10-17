from __future__ import annotations
import json, re
from dataclasses import dataclass, field
from enum import Enum
from .ssh_connection import SSHConnection, quote

# ------------------------------------------------------------------------------
# Machine where build is deployed, its disks and partitioning of selected disk.

MIB = 1024 * 1024
GIB = 1024 * MIB

@dataclass
class TargetDisk:
    path: str
    size: int # Bytes.
    model: str | None = None
    transport: str | None = None
    removable: bool = False
    in_use: bool = False # Has mounted partitions (eg. LiveCD media).
    in_use_reason: str | None = None
    automounted: bool = False # Disk of this computer with partitions mounted by desktop, unmounted before installation.

    @property
    def title(self) -> str:
        return f"{self.path} · {format_size(self.size)}"

    @property
    def subtitle(self) -> str:
        details = [value for value in (self.model, (self.transport or "").upper() or None, "removable" if self.removable else None) if value]
        if self.in_use:
            details.append(self.in_use_reason or "in use, has mounted partitions")
        elif self.automounted:
            details.append("mounted, will be unmounted")
        return ", ".join(details) or "Disk"

    @property
    def external(self) -> bool:
        return self.removable or self.transport in ("usb", "mmc", "sdio", "ieee1394")

@dataclass
class TargetMachine:
    """Details of machine read through SSH connection."""
    architecture: str # uname -m
    uefi: bool
    memory: int # Bytes.
    hostname: str
    disks: list[TargetDisk] = field(default_factory=list)
    # Network used by SSH connection, suggested for static configuration.
    interface: str = ""
    address: str = ""
    gateway: str = ""
    dns: str = ""
    # Disk of this computer, installed system boots on other machine: its firmware is chosen by user and partitions
    # are referenced by UUIDs, as device names differ there.
    local: bool = False
    firmware: TargetFirmware | None = None
    model: str = "" # Device tree model (eg. Raspberry Pi 5 Model B Rev 1.0), empty on machines without device tree.
    image: bool = False # Disk image file (local), its only disk is the image.

    _SCRIPT = r'''
echo "architecture=$(uname -m)"
[ -d /sys/firmware/efi ] && echo "firmware=uefi" || echo "firmware=bios"
echo "memory_kib=$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
echo "hostname=$(hostname)"
echo "model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null)"
echo "disks=$(lsblk -J -b -o NAME,PATH,SIZE,MODEL,TYPE,RO,RM,TRAN,MOUNTPOINTS | tr -d '\n')"
INTERFACE=$(ip -o route get "${SSH_CLIENT%% *}" 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p')
echo "interface=$INTERFACE"
[ -n "$INTERFACE" ] && echo "address=$(ip -o -4 addr show dev "$INTERFACE" | awk '{print $4; exit}')"
echo "gateway=$(ip -4 route show default | awk '{print $3; exit}')"
echo "dns=$(awk '/^nameserver/ {print $2}' /etc/resolv.conf | tr '\n' ' ')"
'''

    @classmethod
    def load(cls, connection: SSHConnection) -> TargetMachine:
        values = dict(line.split("=", 1) for line in connection.output(cls._SCRIPT).splitlines() if "=" in line)
        machine = cls(
            architecture=values.get("architecture", "unknown"),
            uefi=values.get("firmware") == "uefi",
            memory=int(values.get("memory_kib") or 0) * 1024, # Bytes overflow formatting of some awk versions.
            hostname=values.get("hostname", ""),
            interface=values.get("interface", ""),
            address=values.get("address", ""),
            gateway=values.get("gateway", ""),
            dns=values.get("dns", "").strip(),
            model=values.get("model", "").strip(),
        )
        for device in json.loads(values.get("disks") or "{}").get("blockdevices", []):
            if device.get("type") != "disk" or _flag(device.get("ro")) or int(device.get("size") or 0) == 0:
                continue
            machine.disks.append(_target_disk(device, in_use=_has_mountpoints(device)))
        return machine

    @classmethod
    def load_local(cls, connection, architecture: str, firmware: TargetFirmware) -> TargetMachine:
        """Disks of this computer (connection running commands as root here). Architecture is architecture of build,
        as uname -m of machines running it. Disks with mounted system filesystems, swap, LVM, RAID or encrypted
        volumes are in use, partitions mounted by desktop (eg. inserted SD card) are unmounted before installation."""
        output = connection.output("lsblk -J -b -o NAME,PATH,SIZE,MODEL,TYPE,RO,RM,TRAN,MOUNTPOINTS")
        machine = cls(architecture=architecture, uefi=firmware == TargetFirmware.UEFI, memory=0,
                      hostname=connection.host, local=True, firmware=firmware)
        for device in json.loads(output or "{}").get("blockdevices", []):
            if (device.get("type") != "disk" or _flag(device.get("ro")) or int(device.get("size") or 0) == 0
                    or (device.get("name") or "").startswith(("zram", "ram", "nbd"))):
                continue
            reason = _local_in_use_reason(device)
            disk = _target_disk(
                device,
                in_use=reason is not None,
                in_use_reason=reason,
                automounted=_has_mountpoints(device),
            )
            machine.disks.append(disk)
        # External disks first, they are usually the ones to install on.
        machine.disks.sort(key=lambda disk: (disk.in_use, not disk.external))
        return machine

    @classmethod
    def for_image(cls, connection, architecture: str, firmware: TargetFirmware, image_path: str, size: int) -> TargetMachine:
        """Disk image file created through connection (root helper, or virtual machine). Its disk is the image, path of
        loop device is set when it's attached."""
        disk = TargetDisk(path=image_path, size=size, model="Disk image file")
        return cls(architecture=architecture, uefi=firmware == TargetFirmware.UEFI, memory=0, hostname=connection.host,
                   disks=[disk], local=True, firmware=firmware, image=True)

    @property
    def description(self) -> str:
        if self.image:
            return f"Image boots on {self.architecture} machine with {self.firmware.display_name()} firmware"
        if self.local:
            return f"Installed system runs on {self.architecture} machine with {self.firmware.display_name()} firmware"
        firmware = "Raspberry Pi" if self.raspberry_pi else "UEFI" if self.uefi else "BIOS"
        model = f"{self.model}, " if self.model else ""
        return f"{model}{self.architecture}, {firmware} firmware, {format_size(self.memory)} memory"

    @property
    def raspberry_pi(self) -> bool:
        """Machine boots with Raspberry Pi firmware (config.txt, kernel and device tree on FAT /boot partition)."""
        if self.local:
            return self.firmware == TargetFirmware.RASPBERRY_PI
        return "Raspberry Pi" in self.model and not self.uefi

    @property
    def ps3(self) -> bool | None:
        """Machine is PS3, None when it's detected on machine (LiveCD)."""
        return self.firmware == TargetFirmware.PS3 if self.local else None

class TargetFirmware(Enum):
    """Firmware of machine booting disk prepared on this computer."""
    UEFI = "uefi"
    BIOS = "bios"
    PS3 = "ps3"
    PETITBOOT = "petitboot"
    RASPBERRY_PI = "raspberry-pi"
    OTHER = "other"

    def display_name(self) -> str:
        return {
            TargetFirmware.UEFI: "UEFI",
            TargetFirmware.BIOS: "BIOS (legacy boot)",
            TargetFirmware.PS3: "PS3 petitboot",
            TargetFirmware.PETITBOOT: "petitboot",
            TargetFirmware.RASPBERRY_PI: "Raspberry Pi",
            TargetFirmware.OTHER: "other",
        }[self]

    @staticmethod
    def options(architecture: str) -> list[TargetFirmware]:
        if architecture in ("x86_64", "i686", "i586", "i486"):
            return [TargetFirmware.UEFI, TargetFirmware.BIOS]
        if architecture == "ppc64":
            return [TargetFirmware.PS3, TargetFirmware.PETITBOOT]
        if architecture == "ppc64le":
            return [TargetFirmware.PETITBOOT]
        if architecture in RASPBERRY_PI_ARCHITECTURES:
            return [TargetFirmware.UEFI, TargetFirmware.RASPBERRY_PI, TargetFirmware.OTHER]
        return [TargetFirmware.UEFI, TargetFirmware.OTHER]

# Architectures (uname -m) of Raspberry Pi models, where Raspberry Pi firmware boot is offered.
RASPBERRY_PI_ARCHITECTURES = ("aarch64", "armv7l", "armv6l")

# Where desktops mount removable media, such mounts are unmounted before installation. Other mounts mean disk is used
# by this computer.
_AUTOMOUNT_LOCATIONS = ("/media/", "/run/media/")

def _local_in_use_reason(device: dict) -> str | None:
    for mountpoint in device.get("mountpoints") or []:
        if not mountpoint:
            continue
        if mountpoint == "[SWAP]":
            return "in use, has active swap"
        if not mountpoint.startswith(_AUTOMOUNT_LOCATIONS):
            return f"in use, mounted at {mountpoint}"
    if device.get("type") not in ("disk", "part"):
        return f"in use by {device.get('type')} volume"
    for child in device.get("children") or []:
        if reason := _local_in_use_reason(child):
            return reason
    return None

def _flag(value) -> bool:
    return value in (True, 1, "1", "true")

def _target_disk(device: dict, **state) -> TargetDisk:
    return TargetDisk(
        path=device.get("path") or f"/dev/{device['name']}",
        size=int(device["size"]),
        model=(device.get("model") or "").strip() or None,
        transport=device.get("tran"),
        removable=_flag(device.get("rm")),
        **state,
    )

def _has_mountpoints(device: dict) -> bool:
    if any(device.get("mountpoints") or []):
        return True
    return any(_has_mountpoints(child) for child in device.get("children") or [])

def format_size(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024

# Architectures of builds, as uname -m of machines that can run them.
_MACHINE_ARCHITECTURES = {
    "amd64": {"x86_64"},
    "x86": {"i686", "i586", "i486", "x86_64"},
    "arm64": {"aarch64"},
    "arm": {"armv7l", "armv6l", "armv5tel", "aarch64"},
    "ppc64": {"ppc64"},
    "ppc64le": {"ppc64le"},
    "riscv": {"riscv64"},
}

def machine_architecture(build_architecture: str | None) -> str | None:
    """uname -m of machine running build of given architecture."""
    return {"amd64": "x86_64", "x86": "i686", "arm64": "aarch64", "arm": "armv7l", "ppc64": "ppc64", "ppc64le": "ppc64le",
            "riscv": "riscv64"}.get(build_architecture or "")

def architecture_supported(build_architecture: str | None, machine_architecture: str) -> bool | None:
    """Whether machine can run build of given architecture, None when unknown."""
    supported = _MACHINE_ARCHITECTURES.get(build_architecture or "")
    return None if supported is None else machine_architecture in supported

# ------------------------------------------------------------------------------
# Partitioning: new partition table (GPT or MBR) with partitions defined by user, in order. Last partition can
# use remaining space of disk. MBR tables have only primary partitions (up to 4).

class PartitionTable(Enum):
    GPT = "gpt"
    MBR = "dos"

    @property
    def display_name(self) -> str:
        return {PartitionTable.GPT: "GPT", PartitionTable.MBR: "MBR (DOS)"}[self]

MBR_MAX_PARTITIONS = 4
MBR_MAX_SIZE = 2 * 1024 * GIB

class PartitionType(Enum):
    EFI = "efi"
    BIOS_BOOT = "bios-boot"
    LINUX = "linux"
    SWAP = "swap"
    FIRMWARE = "firmware" # FAT partition read by firmware (Raspberry Pi), mounted at /boot.

    @property
    def display_name(self) -> str:
        return {PartitionType.EFI: "EFI system", PartitionType.BIOS_BOOT: "BIOS boot", PartitionType.LINUX: "Linux filesystem",
                PartitionType.SWAP: "Swap", PartitionType.FIRMWARE: "Firmware boot (FAT)"}[self]

    @property
    def gpt_type(self) -> str:
        return {
            PartitionType.EFI: "C12A7328-F81F-11D2-BA4B-00A0C93EC93B",
            PartitionType.BIOS_BOOT: "21686148-6449-6E6F-744E-656564454649",
            PartitionType.LINUX: "0FC63DAF-8483-4772-8E79-3D69D7984743",
            PartitionType.SWAP: "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F",
            PartitionType.FIRMWARE: "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7", # Basic data, FAT.
        }[self]

    @property
    def mbr_type(self) -> str | None:
        return {PartitionType.EFI: "ef", PartitionType.LINUX: "83", PartitionType.SWAP: "82", PartitionType.FIRMWARE: "c"}.get(self)

# Partitions with filesystem mounted in installed system.
MOUNTED_TYPES = (PartitionType.LINUX, PartitionType.EFI, PartitionType.FIRMWARE)

class TargetFilesystem(Enum):
    EXT4 = "ext4"
    XFS = "xfs"
    BTRFS = "btrfs"
    VFAT = "vfat"

    def format_command(self, label: str) -> str:
        label = quote(label[:11].upper() if self == TargetFilesystem.VFAT else label[:16])
        return {
            TargetFilesystem.EXT4: f"mkfs.ext4 -F -L {label}",
            TargetFilesystem.XFS: f"mkfs.xfs -f -L {label}",
            TargetFilesystem.BTRFS: f"mkfs.btrfs -f -L {label}",
            TargetFilesystem.VFAT: f"mkfs.vfat -F 32 -n {label}",
        }[self]

@dataclass
class PartitionSpec:
    """Partition defined by user."""
    type: PartitionType
    size: int | None # Bytes, None uses remaining space of disk.
    filesystem: TargetFilesystem | None = None # Linux filesystem partitions.
    mount_point: str | None = None # Linux filesystem and EFI partitions.

    @property
    def effective_filesystem(self) -> TargetFilesystem | None:
        match self.type:
            case PartitionType.EFI | PartitionType.FIRMWARE: return TargetFilesystem.VFAT
            case PartitionType.LINUX: return self.filesystem or TargetFilesystem.EXT4
        return None

    @property
    def label(self) -> str:
        """Name of partition and filesystem label."""
        match self.type:
            case PartitionType.EFI: return "EFI"
            case PartitionType.FIRMWARE: return "boot"
            case PartitionType.BIOS_BOOT: return "BIOS boot"
            case PartitionType.SWAP: return "swap"
        return "root" if self.mount_point == "/" else (self.mount_point or "data").strip("/").replace("/", "-")

@dataclass
class TargetPartition:
    name: str
    size: int | None # Bytes, None for the rest of disk.
    type: str # GPT partition type GUID.
    mount_point: str | None # None for swap and BIOS boot.
    filesystem: str | None # fstab type.
    format_command: str | None
    spec: PartitionSpec | None = None

_MOUNT_POINT = re.compile(r"^/([A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*)?$")

def default_image_layout(uefi: bool, architecture: str, table: PartitionTable = PartitionTable.GPT,
                         raspberry_pi: bool = False) -> list[PartitionSpec]:
    """Disk image: boot partition and root using the rest. Root is last, so it can grow to fill disk image is written
    to, and there's no swap or /home."""
    layout = []
    if raspberry_pi:
        layout.append(PartitionSpec(PartitionType.FIRMWARE, 512 * MIB, mount_point="/boot"))
    elif uefi:
        layout.append(PartitionSpec(PartitionType.EFI, 512 * MIB, mount_point="/efi"))
    elif architecture in ("x86_64", "i686", "i586", "i486") and table == PartitionTable.GPT:
        layout.append(PartitionSpec(PartitionType.BIOS_BOOT, 1 * MIB))
    layout.append(PartitionSpec(PartitionType.LINUX, None, TargetFilesystem.EXT4, "/"))
    return layout

def default_layout(disk: TargetDisk, uefi: bool, memory: int, architecture: str,
                   table: PartitionTable = PartitionTable.GPT, raspberry_pi: bool = False) -> list[PartitionSpec]:
    """EFI system (UEFI) or BIOS boot partition (x86 BIOS with GPT), /boot, swap (size of memory, up to 8 GiB), root
    and /home. Small disks, and MBR tables without space for more partitions, get no /home, root uses remaining space
    then. Raspberry Pi firmware reads /boot, which is FAT partition then."""
    layout = []
    if raspberry_pi:
        layout.append(PartitionSpec(PartitionType.FIRMWARE, 512 * MIB, mount_point="/boot"))
    elif uefi:
        layout.append(PartitionSpec(PartitionType.EFI, 1 * GIB, mount_point="/efi"))
    elif architecture in ("x86_64", "i686", "i586", "i486") and table == PartitionTable.GPT:
        layout.append(PartitionSpec(PartitionType.BIOS_BOOT, 1 * MIB))
    if not raspberry_pi:
        layout.append(PartitionSpec(PartitionType.LINUX, 1 * GIB, TargetFilesystem.EXT4, "/boot"))
    swap = min(8 * GIB, max(1 * GIB, round(memory / GIB) * GIB)) if memory else 2 * GIB
    disk_size = min(disk.size, MBR_MAX_SIZE) if table == PartitionTable.MBR else disk.size
    available = disk_size - sum(partition.size for partition in layout) - 8 * MIB
    if available - swap < 24 * GIB:
        swap = 0 if available < 16 * GIB else min(swap, 2 * GIB)
    if swap:
        layout.append(PartitionSpec(PartitionType.SWAP, swap))
    available -= swap
    free_slots = MBR_MAX_PARTITIONS - len(layout) if table == PartitionTable.MBR else None
    if available >= 96 * GIB and (free_slots is None or free_slots >= 2):
        # Root gets 30% of space (between 48 and 256 GiB), the rest is for /home.
        root = min(256 * GIB, max(48 * GIB, int(available * 0.3) // GIB * GIB))
        layout.append(PartitionSpec(PartitionType.LINUX, root, TargetFilesystem.EXT4, "/"))
        layout.append(PartitionSpec(PartitionType.LINUX, None, TargetFilesystem.EXT4, "/home"))
    else:
        layout.append(PartitionSpec(PartitionType.LINUX, None, TargetFilesystem.EXT4, "/"))
    return layout

@dataclass
class PartitionPlan:
    disk: TargetDisk
    uefi: bool
    layout: list[PartitionSpec] = field(default_factory=list)
    table: PartitionTable = PartitionTable.GPT
    root: str = "/mnt/gentoo" # Where root filesystem is mounted during installation.
    raspberry_pi: bool = False # Boots with Raspberry Pi firmware from FAT /boot partition.
    local: bool = False # Disk of this computer, only its own partitions are unmounted (not all swap and mounts).
    image_path: str | None = None # Disk image file, disk is its loop device when attached.
    compress_image: bool = True # Image is compressed (.img.xz) when it's done.

    @property
    def image(self) -> bool:
        return self.image_path is not None

    @property
    def root_index(self) -> int | None:
        return self.index_of(lambda spec: spec.type == PartitionType.LINUX and spec.mount_point == "/")

    @property
    def partitions(self) -> list[TargetPartition]:
        partitions = []
        for spec in self.layout:
            filesystem = spec.effective_filesystem
            fstab_type = "swap" if spec.type == PartitionType.SWAP else filesystem.value if filesystem else None
            if spec.type == PartitionType.SWAP:
                command = "mkswap -L swap"
            elif filesystem:
                command = filesystem.format_command(spec.label)
            else:
                command = None
            mount_point = spec.mount_point if spec.type in MOUNTED_TYPES else None
            partition_type = spec.type.gpt_type if self.table == PartitionTable.GPT else spec.type.mbr_type
            partitions.append(TargetPartition(spec.label, spec.size, partition_type, mount_point, fstab_type, command, spec))
        return partitions

    @property
    def fixed_size(self) -> int:
        return sum(spec.size for spec in self.layout if spec.size)

    @property
    def usable_size(self) -> int:
        """Disk size without partition table and alignment. MBR addresses only first 2 TiB."""
        size = min(self.disk.size, MBR_MAX_SIZE) if self.table == PartitionTable.MBR else self.disk.size
        return size - 4 * MIB

    @property
    def remaining_size(self) -> int:
        return self.usable_size - self.fixed_size

    def size_of(self, spec: PartitionSpec) -> int:
        return spec.size if spec.size else max(0, self.remaining_size)

    @property
    def efi_mount_point(self) -> str | None:
        return next((spec.mount_point for spec in self.layout if spec.type == PartitionType.EFI and spec.mount_point), None)

    def index_of(self, predicate) -> int | None:
        """Partition number (1-based) of first partition matching predicate."""
        return next((index for index, spec in enumerate(self.layout, start=1) if predicate(spec)), None)

    def error(self) -> str | None:
        if not self.layout:
            return "Add partitions"
        roots = [spec for spec in self.layout if spec.type == PartitionType.LINUX and spec.mount_point == "/"]
        if len(roots) != 1:
            return "Exactly one Linux filesystem partition must be mounted at /"
        mount_points = [spec.mount_point for spec in self.layout if spec.type in MOUNTED_TYPES]
        for mount_point in mount_points:
            if not mount_point:
                return "Set mount point of every partition with filesystem"
            if not _MOUNT_POINT.match(mount_point) or "/.." in mount_point or "/./" in mount_point:
                return f"Invalid mount point {mount_point}"
        if len(set(mount_points)) != len(mount_points):
            return "Mount points must be different"
        for index, spec in enumerate(self.layout):
            if spec.size is None and index != len(self.layout) - 1:
                return "Only last partition can use remaining space"
            if spec.size is not None and spec.size < 1 * MIB:
                return "Partitions must have at least 1 MiB"
        if self.remaining_size < (1 * GIB if self.layout[-1].size is None else 0):
            return f"Partitions don't fit on disk, {format_size(self.usable_size)} available"
        if self.image:
            root = self.layout[self.root_index - 1]
            if self.root_index != len(self.layout) or root.size is not None or root.effective_filesystem != TargetFilesystem.EXT4:
                return "Root partition of image must be last ext4 partition using remaining space, it's shrunk to contents and grows to fill disk"
        if self.raspberry_pi and not any(spec.type == PartitionType.FIRMWARE and spec.mount_point == "/boot" for spec in self.layout):
            return "Raspberry Pi needs firmware boot (FAT) partition mounted at /boot"
        efi = [spec for spec in self.layout if spec.type == PartitionType.EFI]
        if self.uefi and not efi:
            return "UEFI machine needs EFI system partition"
        if len(efi) > 1:
            return "Only one EFI system partition can be created"
        if any(spec.size and spec.size < 64 * MIB for spec in efi):
            return "EFI system partition needs at least 64 MiB"
        if self.table == PartitionTable.MBR:
            if len(self.layout) > MBR_MAX_PARTITIONS:
                return f"MBR table can have up to {MBR_MAX_PARTITIONS} partitions, use GPT for more"
            if any(spec.type == PartitionType.BIOS_BOOT for spec in self.layout):
                return "BIOS boot partition is used only with GPT"
        return None

    def warnings(self, architecture: str) -> list[str]:
        warnings = []
        if self.table == PartitionTable.MBR and self.disk.size > MBR_MAX_SIZE:
            warnings.append(f"MBR uses only first {format_size(MBR_MAX_SIZE)} of disk")
        if (self.table == PartitionTable.GPT and not self.uefi and architecture in ("x86_64", "i686", "i586", "i486")
                and not any(spec.type == PartitionType.BIOS_BOOT for spec in self.layout)):
            warnings.append("GRUB on BIOS machine needs BIOS boot partition")
        root = next((spec for spec in self.layout if spec.mount_point == "/" and spec.type == PartitionType.LINUX), None)
        if root and self.size_of(root) < 16 * GIB and not self.image:
            warnings.append("Root partition is small")
        return warnings

    def partition_device(self, index: int) -> str:
        """Device of partition (1-based), eg. /dev/sda1 or /dev/nvme0n1p1."""
        separator = "p" if re.search(r"\d$", self.disk.path) else ""
        return f"{self.disk.path}{separator}{index}"

    def sfdisk_script(self) -> str:
        lines = [f"label: {self.table.value}"]
        # On MBR, partition with /boot (or root) is marked active, older BIOSes boot only from it.
        bootable = None
        if self.table == PartitionTable.MBR:
            bootable = self.index_of(lambda spec: spec.mount_point == "/boot") or self.index_of(lambda spec: spec.mount_point == "/")
        for index, partition in enumerate(self.partitions, start=1):
            size = partition.size
            if size is None and self.table == PartitionTable.MBR and self.disk.size > MBR_MAX_SIZE:
                size = self.remaining_size # Remaining space would reach past addressable part of disk.
            fields = [f"size={size // MIB}MiB"] if size else []
            fields.append(f"type={partition.type}")
            if self.table == PartitionTable.GPT:
                fields.append(f'name="{partition.name}"')
            if index == bootable:
                fields.append("bootable")
            lines.append(", ".join(fields))
        return "\n".join(lines) + "\n"

    # Scripts run on machine:

    def release_script(self) -> str:
        """Unmounts partitions of disk and disables their swap. On LiveCD all of them, on this computer only partitions
        of selected disk (mounted by desktop, eg. when SD card was inserted or new partitions appeared)."""
        if not self.local:
            return f"swapoff -a 2>/dev/null || true\numount -R {quote(self.root)} 2>/dev/null || true\n"
        return f"""for PART in $(lsblk -lnpo NAME {quote(self.disk.path)}); do
    swapoff "$PART" 2>/dev/null || true
    while findmnt -rn -S "$PART" > /dev/null; do echo "Unmounting $PART"; umount "$PART" || exit 1; done
done
"""

    def partition_script(self) -> str:
        disk = quote(self.disk.path)
        return f"""set -e
{self.release_script()}echo "Removing old partitions and signatures from {self.disk.path}"
wipefs -a {disk}
sfdisk --wipe always --wipe-partitions always {disk} <<'SFDISK'
{self.sfdisk_script()}SFDISK
partprobe {disk} 2>/dev/null || blockdev --rereadpt {disk} 2>/dev/null || true
udevadm settle 2>/dev/null || sleep 2
sfdisk -l {disk}
"""

    def format_script(self) -> str:
        lines = ["set -e", self.release_script()]
        for index, partition in enumerate(self.partitions, start=1):
            if partition.format_command:
                device = quote(self.partition_device(index))
                lines.append(f'echo "Formatting {partition.name} partition ({partition.filesystem})"')
                lines.append(f"{partition.format_command} {device}")
        return "\n".join(lines) + "\n"

    def mount_script(self) -> str:
        lines = ["set -e", self.release_script(), f"mkdir -p {quote(self.root)}"]
        # Parents are mounted before their subdirectories.
        mounted = sorted(
            ((index, partition) for index, partition in enumerate(self.partitions, start=1) if partition.mount_point),
            key=lambda item: item[1].mount_point.rstrip("/").count("/") if item[1].mount_point != "/" else 0
        )
        for index, partition in mounted:
            target = self.root + (partition.mount_point if partition.mount_point != "/" else "")
            device = self.partition_device(index)
            lines.append(f"mkdir -p {quote(target)}")
            lines.append(f'echo "Mounting {device} ({partition.mount_point}) at {target}"')
            lines.append(f"mount {quote(device)} {quote(target)}")
        # All mounted filesystems of installed system, not only root.
        lines.append(f"df -h $(findmnt -R -l -n -o TARGET {quote(self.root)})")
        return "\n".join(lines) + "\n"

    def fstab_script(self) -> str:
        """Appends partitions to /etc/fstab of installed system, using UUIDs."""
        lines = ["set -e", f"FSTAB={quote(self.root)}/etc/fstab", 'echo "" >> "$FSTAB"', 'echo "# Added by Catalyst Lab" >> "$FSTAB"']
        for index, partition in enumerate(self.partitions, start=1):
            if not partition.filesystem:
                continue
            device = quote(self.partition_device(index))
            if partition.filesystem == "swap":
                entry = 'none swap sw 0 0'
            elif partition.mount_point == "/":
                entry = f"/ {partition.filesystem} defaults,noatime 0 1"
            elif partition.filesystem == "vfat":
                entry = f"{partition.mount_point} vfat defaults,noatime,umask=0077 0 2"
            else:
                entry = f"{partition.mount_point} {partition.filesystem} defaults,noatime 0 2"
            lines.append(f'echo "UUID=$(blkid -s UUID -o value {device}) {entry}" >> "$FSTAB"')
        lines.append('cat "$FSTAB"')
        return "\n".join(lines) + "\n"

    def required_tools(self) -> list[str]:
        tools = ["wipefs", "sfdisk", "blkid", "tar", "chroot", "findmnt"] + (["lsblk"] if self.local else [])
        if self.image:
            tools += ["losetup", "truncate", "e2fsck", "resize2fs", "dumpe2fs"] + (["xz"] if self.compress_image else [])
        tools += sorted({partition.format_command.split()[0] for partition in self.partitions if partition.format_command})
        return tools
