from __future__ import annotations
import gzip, hashlib, json, os, re, shutil, socket, subprocess, sys, tempfile, threading
from pathlib import Path
from gi.repository import GLib
from dataclasses import dataclass, field
from enum import Enum, auto
from .event_bus import EventBus
from .multistage_process import (
    MultiStageProcess, MultiStageProcessStage, MultiStageProcessStageState,
    MultiStageProcessEvent, MultiStageProcessState,
)
from .custom_kernel import KERNEL_OUTPUTS_MANIFEST, copy_kernel_outputs, is_arm64_linux_image

# ------------------------------------------------------------------------------
# Running builds in QEMU: live CD ISO images (booted by firmware of machine), and stages with generic kernel (stage
# kernel and initramfs started by QEMU, system in a temporary disk image made from stage archive). Display is served over VNC on
# local socket and shown in the app, serial console and QEMU monitor (QMP) use sockets too.
# Machine of the same architecture as this computer is accelerated (HVF on macOS, KVM on Linux), others are emulated.
# Whether a build boots depends on its architecture and kernel support for the emulated machine. Do not infer target
# compatibility from device or package names: the app supports projects beyond any specific board.

_QEMU_SEARCH_PATHS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")
_FIRMWARE_DIRECTORIES = ("/opt/homebrew/share/qemu", "/usr/local/share/qemu", "/usr/share/qemu", "/usr/share/edk2",
                         "/usr/share/edk2-ovmf", "/usr/share/AAVMF", "/usr/share/qemu-efi-aarch64",
                         "/usr/share/AAVMF")

def _new_run_directory(backend_name: str) -> str:
    """Create VM state in Library/Temporary for Apple VMs; keep QEMU sockets in short /tmp paths."""
    root = "/tmp"
    if backend_name == "Apple Virtualization":
        try:
            from .repository import Repository, resolve_path
            temporary = resolve_path(Repository.Settings.value.temporary_location)
            root = os.path.join(temporary, "VMs")
            os.makedirs(root, exist_ok=True)
        except AttributeError:
            # Tests and early setup may construct a run before Library settings are registered.
            root = tempfile.gettempdir()
    return tempfile.mkdtemp(prefix="cl-qemu-", dir=root)

@dataclass
class QemuMachine:
    """How QEMU runs machine of architecture."""
    binary: str
    machine: list[str]           # Machine type and its options.
    console: str                 # Serial console device of kernel.
    cpu_emulated: str            # CPU when emulated (accelerated machine uses host CPU).
    firmware: list[str] = field(default_factory=list) # UEFI firmware file names (first found is used), empty for BIOS.
    display: list[str] = field(default_factory=lambda: ["-device", "virtio-gpu-pci"])
    disk_interface: str = "virtio"
    disk_device: str | None = None
    device_bus: str = "pci"
    usb: bool = True
    host_architecture: str = ""  # platform.machine() of computers that run it accelerated.

_MACHINES = {
    "arm64": QemuMachine(binary="qemu-system-aarch64", machine=["-machine", "virt"], console="ttyAMA0", cpu_emulated="max",
                         firmware=["edk2-aarch64-code.fd", "AAVMF_CODE.fd", "QEMU_EFI.fd"], host_architecture="arm64|aarch64"),
    "arm": QemuMachine(binary="qemu-system-arm", machine=["-machine", "virt"], console="ttyAMA0", cpu_emulated="cortex-a15",
                       display=["-device", "virtio-gpu-device"], device_bus="device"),
    "amd64": QemuMachine(binary="qemu-system-x86_64", machine=["-machine", "q35"], console="ttyS0", cpu_emulated="max",
                         display=["-device", "virtio-vga"], host_architecture="x86_64|amd64"),
    "x86": QemuMachine(binary="qemu-system-i386", machine=["-machine", "pc"], console="ttyS0", cpu_emulated="max",
                       display=["-device", "VGA"], host_architecture=""),
    "ppc": QemuMachine(binary="qemu-system-ppc", machine=["-machine", "40p"], console="ttyS0", cpu_emulated="604",
                       display=["-device", "VGA"]),
    "ppc64": QemuMachine(binary="qemu-system-ppc64", machine=["-machine", "pseries"], console="hvc0", cpu_emulated="POWER8",
                         display=["-device", "virtio-gpu-pci"]),
    "ppc64le": QemuMachine(binary="qemu-system-ppc64", machine=["-machine", "pseries"], console="hvc0", cpu_emulated="POWER8",
                           display=["-device", "virtio-gpu-pci"]),
    "riscv": QemuMachine(binary="qemu-system-riscv64", machine=["-machine", "virt"], console="ttyS0", cpu_emulated="max",
                         display=["-device", "virtio-gpu-device"], device_bus="device"),
    "s390x": QemuMachine(binary="qemu-system-s390x", machine=["-machine", "s390-ccw-virtio"], console="ttysclp0", cpu_emulated="qemu",
                         display=["-device", "virtio-gpu-ccw"], disk_interface="none", disk_device="virtio-blk-ccw",
                         device_bus="ccw", usb=False),
}

def qemu_binary(name: str) -> str | None:
    return shutil.which(name) or next((os.path.join(path, name) for path in _QEMU_SEARCH_PATHS
                                       if os.access(os.path.join(path, name), os.X_OK)), None)

def install_hint() -> str:
    if sys.platform == "darwin":
        return "brew install qemu"
    if sys.platform.startswith("linux"):
        return "install the QEMU system emulator for this architecture using your distribution's package manager"
    return "install QEMU for this platform"

def _firmware_path(names: list[str]) -> str | None:
    for directory in _FIRMWARE_DIRECTORIES:
        for name in names:
            if os.path.isfile(os.path.join(directory, name)):
                return os.path.join(directory, name)
        # Linux packages commonly put architecture-specific firmware one or two levels below these roots.
        if os.path.isdir(directory):
            for root, dirs, files in os.walk(directory):
                relative_depth = os.path.relpath(root, directory).count(os.sep)
                if relative_depth >= 2:
                    dirs[:] = []
                for name in names:
                    if name in files:
                        return os.path.join(root, name)
    return None

# ------------------------------------------------------------------------------
# What can run.

class QemuBuildKind(Enum):
    ISO = auto()
    STAGE = auto()

@dataclass
class QemuSupport:
    kind: QemuBuildKind | None
    machine: QemuMachine | None
    kernel: str | None = None     # Stage kernel and initramfs, in /boot.
    initramfs: str | None = None
    reason: str | None = None     # Why build can't run.
    backend: str = "qemu"
    kernel_package: str | None = None # Emerged into the temporary guest image before boot.
    kernel_snapshot: object | None = None # Gentoo repository snapshot used to emerge the selected package.
    kernel_toolset: object | None = None # Toolset used for this run when the project's toolset is busy.
    image_directory: str | None = None # Per-run Library/Temporary workspace, removed when the VM stops.
    base_image_path: str | None = None # Immutable cached raw disk used as a QEMU qcow2 backing image.
    base_kernel_path: str | None = None # Cached boot kernel used directly by QEMU.
    base_initramfs_path: str | None = None # Cached initramfs used directly by QEMU.

    @property
    def boot_kernel(self) -> str | None:
        return "custom-kernel" if self.kernel_package else self.kernel

    @property
    def boot_initramfs(self) -> str | None:
        return "custom-initramfs" if self.kernel_package else self.initramfs

    @property
    def available(self) -> bool:
        return self.reason is None

def kernel_build_toolsets(project_directory) -> list:
    """Free kernel-build toolsets, with the project's configured toolset first when available."""
    project_toolset = project_directory.get_toolset()
    from .repository import Repository
    from .toolset import ToolsetEnv
    toolsets = ([project_toolset] if project_toolset and not project_toolset.busy_reason else [])
    toolsets.extend(toolset for toolset in Repository.Toolset.value
                    if toolset is not project_toolset and toolset.env == ToolsetEnv.EXTERNAL
                    and not toolset.busy_reason)
    return toolsets

_KERNEL = re.compile(r"\./boot/((vmlinuz|vmlinux|Image)-?[^/]*)$")
_INITRAMFS = re.compile(r"\./boot/((initramfs|initrd)-?[^/]*)$")
_ISO_LINUX = re.compile(r"^\s*linux(?:efi)?\s+([\"']?)([^\s\"']+)\1", re.MULTILINE)

_KERNEL_ARCHITECTURES = {
    "amd64": {0x8664},
    "x86": {0x014C},
    "arm64": {0xAA64},
    "arm": {0x01C0, 0x01C2},
    "ppc": {0x01F0},
    "ppc64": {0x01F2},
    "ppc64le": {0x01F2},
    "riscv": {0x5032, 0x5064},
    "s390x": {0xA641},
}

def _iso_kernel_architecture(iso_path: str) -> str | None:
    """Read the architecture of a kernel selected by the ISO's GRUB config.

    Linux EFI-stub kernels are PE/COFF images. This inspects the small header
    after streaming the kernel from the ISO; it does not unpack the live image.
    """
    try:
        config = subprocess.run(["bsdtar", "-xOf", iso_path, "boot/grub/grub.cfg"],
                                check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    for match in _ISO_LINUX.finditer(config):
        kernel_path = match.group(2).lstrip("/")
        try:
            kernel = subprocess.run(["bsdtar", "-xOf", iso_path, kernel_path],
                                    check=True, capture_output=True).stdout
        except (OSError, subprocess.CalledProcessError):
            continue
        if len(kernel) >= 0x40 and kernel[:2] == b"MZ":
            pe_offset = int.from_bytes(kernel[0x3C:0x40], "little")
            if kernel[pe_offset:pe_offset + 4] == b"PE\0\0":
                machine = int.from_bytes(kernel[pe_offset + 4:pe_offset + 6], "little")
                for architecture, machines in _KERNEL_ARCHITECTURES.items():
                    if machine in machines:
                        return architecture
        # ELF kernels are uncommon in ISO boot menus, but are used by several
        # non-x86 Linux boot paths. Inspect only the ELF identification/header.
        if len(kernel) >= 20 and kernel[:4] == b"\x7fELF":
            endian = "little" if kernel[5] == 1 else "big" if kernel[5] == 2 else None
            if endian:
                machine = int.from_bytes(kernel[18:20], endian)
                elf_architectures = {
                    3: "x86", 62: "amd64", 40: "arm", 183: "arm64",
                    20: "ppc", 21: "ppc64le" if endian == "little" else "ppc64",
                    243: "riscv", 22: "s390x",
                }
                if architecture := elf_architectures.get(machine):
                    return architecture
        # ARM32 zImage magic (little-endian 0x016f2818 at offset 0x24).
        if len(kernel) > 0x28 and kernel[0x24:0x28] == b"\x18\x28\x6f\x01":
            return "arm"
        # ARM64 Image and RISC-V Image boot header magic values.
        if len(kernel) >= 0x40 and kernel[0x38:0x3C] == b"ARM\x64":
            return "arm64"
        if len(kernel) >= 0x38 and kernel[0x30:0x38] == b"RISCV\0\0\0":
            return "riscv"
        # Linux x86 boot protocol signature, used by non-PE bzImage kernels.
        if len(kernel) > 0x208 and kernel[0x202:0x206] == b"HdrS":
            return "amd64" if kernel[0x206] >= 0x20 else "x86"
    return None

def _stage_boot_files(build) -> tuple[list[str], list[str]]:
    """Kernels and initramfs images in /boot of stage, from listing of archive made by catalyst."""
    kernels, initramfs = [], []
    listing = (build.artifact_path or "") + ".CONTENTS.gz"
    if not os.path.isfile(listing):
        return kernels, initramfs
    with gzip.open(listing, "rt", encoding="utf-8", errors="replace") as file:
        for line in file:
            line = line.rstrip("\n").split(" -> ")[0]
            if match := _KERNEL.search(line):
                kernels.append(match.group(1))
            elif match := _INITRAMFS.search(line):
                initramfs.append(match.group(1))
    return kernels, initramfs

def qemu_support(project_directory, build, *, check_runtime: bool = True) -> QemuSupport:
    """Whether build can run in QEMU, how, or why not."""
    from .project_build import StageBuildStatus
    from .deploy_live_iso import is_live_iso_build
    from .deploy_installation import is_deployable
    architecture = project_directory.get_architecture() if project_directory else None
    machine = _MACHINES.get(architecture.value) if architecture else None
    if build.status != StageBuildStatus.COMPLETED or not build.artifact or not os.path.isfile(build.artifact_path or ""):
        return QemuSupport(None, machine, reason="Build has no result")
    if is_live_iso_build(build):
        kind = QemuBuildKind.ISO
        kernel = initramfs = None
        if machine is None:
            return QemuSupport(kind, None, reason=f"Running {architecture.value if architecture else 'this architecture'} in QEMU isn't supported yet")
        if qemu_binary("bsdtar") is None:
            return QemuSupport(kind, machine, reason="ISO inspection requires bsdtar (libarchive tools)")
        kernel_architecture = _iso_kernel_architecture(build.artifact_path)
        if kernel_architecture is None:
            return QemuSupport(kind, machine, reason="ISO does not contain a supported Linux kernel")
        if kernel_architecture != architecture.value:
            return QemuSupport(kind, machine, reason=f"ISO kernel architecture ({kernel_architecture}) does not match build architecture ({architecture.value})")
    elif is_deployable(project_directory, build):
        kind = QemuBuildKind.STAGE
        kernels, images = _stage_boot_files(build)
        if machine is None:
            return QemuSupport(kind, None, reason=f"Running {architecture.value if architecture else 'this architecture'} in QEMU isn't supported yet")
        # Kernel filenames are user-defined by Catalyst. Use the newest available kernel and let QEMU determine
        # whether it supports the selected virtual machine instead of guessing from target-specific names. A stage
        # with no kernel can still run after the launch wizard builds one into its disposable VM image.
        kernel = sorted(kernels)[-1] if kernels else None
        version = re.sub(r"^(vmlinuz|vmlinux|Image)-?", "", kernel) if kernel else ""
        initramfs = next((image for image in images if version and version in image), images[0] if images else None)
    else:
        return QemuSupport(None, machine, reason="Only stage3, stage4 and live CD builds can run")
    if not check_runtime:
        return QemuSupport(kind, machine, kernel=kernel, initramfs=initramfs)
    if qemu_binary(machine.binary) is None:
        return QemuSupport(kind, machine, reason=f"QEMU for this architecture is not installed ({install_hint()})")
    if kind == QemuBuildKind.STAGE and qemu_binary("qemu-img") is None:
        return QemuSupport(kind, machine, reason="Stage boot requires qemu-img, which is not installed")
    if machine.firmware and _firmware_path(machine.firmware) is None and kind == QemuBuildKind.ISO:
        return QemuSupport(kind, machine, reason="UEFI firmware of QEMU is not found")
    return QemuSupport(kind, machine, kernel=kernel, initramfs=initramfs)

def accelerated(machine: QemuMachine) -> str | None:
    """Accelerator when machine has architecture of this computer."""
    import platform
    if not machine.host_architecture or not re.fullmatch(machine.host_architecture, platform.machine().lower()):
        return None
    if sys.platform == "darwin":
        return "hvf"
    if sys.platform.startswith("linux") and os.access("/dev/kvm", os.R_OK | os.W_OK):
        return "kvm"
    return None

# ------------------------------------------------------------------------------
# Cached pristine stage disk images are copied into a per-run Temporary workspace before boot.

def stage_image_cache_directory(build, support: QemuSupport, size: int) -> str:
    """Stable cache location for the pristine image derived from this exact stage build."""
    from .repository import Repository, resolve_path
    artifact = build.artifact_path
    signature = [str(build.id), os.path.realpath(artifact), str(size),
                 support.kernel or "", support.initramfs or ""]
    try:
        stat = os.stat(artifact)
        signature.append(f"{stat.st_size}:{stat.st_mtime_ns}")
    except OSError:
        signature.append("missing")
    key = hashlib.sha256("\n".join(signature).encode()).hexdigest()[:24]
    cache = resolve_path(Repository.Settings.value.cache_location)
    return os.path.join(cache, "TestVMImages", key)

def stage_base_image_ready(directory: str, kernel: str | None, initramfs: str | None) -> bool:
    """A cache entry is usable only after image creation completed and was marked ready."""
    files = ["root.img"] + ([kernel] if kernel else []) + ([initramfs] if initramfs else [])
    return (os.path.isfile(os.path.join(directory, ".ready"))
            and all(os.path.isfile(os.path.join(directory, name)) for name in files))

def _clone_or_sparse_copy(source: str, destination: str, process_holder: list | None = None,
                          cancel_event=None) -> bool:
    """Clone a large image instantly when the filesystem supports it; otherwise preserve sparse holes."""
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("VM startup canceled")
    def run_copy(command, quiet=False):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("VM startup canceled")
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL if quiet else None)
        if process_holder is not None:
            process_holder.append(process)
        try:
            status = process.wait()
        finally:
            if process_holder is not None and process in process_holder:
                process_holder.remove(process)
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("VM startup canceled")
        if status:
            raise subprocess.CalledProcessError(status, command)

    if sys.platform == "darwin":
        import ctypes
        clonefile = ctypes.CDLL(None, use_errno=True).clonefile
        clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)
        clonefile.restype = ctypes.c_int
        if clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0:
            return True
    elif sys.platform.startswith("linux"):
        import fcntl
        # Linux FICLONE asks the filesystem to share extents copy-on-write.
        try:
            with open(source, "rb") as source_file, open(destination, "xb") as destination_file:
                fcntl.ioctl(destination_file.fileno(), 0x40049409, source_file.fileno())
            return True
        except OSError:
            try:
                os.unlink(destination)
            except FileNotFoundError:
                pass

    try:
        if sys.platform.startswith("linux"):
            run_copy(["cp", "--reflink=always", "--sparse=always", "--preserve=mode,timestamps",
                      source, destination])
        else:
            # BSD cp requests clonefile on APFS; if unsupported it reports failure and we use a regular copy.
            run_copy(["cp", "-c", "-p", source, destination], quiet=True)
            return True
    except (OSError, subprocess.CalledProcessError):
        try:
            os.unlink(destination)
        except FileNotFoundError:
            pass
        if sys.platform == "darwin":
            run_copy(["cp", "-p", source, destination])
        elif sys.platform.startswith("linux"):
            run_copy(["cp", "--sparse=always", "--preserve=mode,timestamps", source, destination])
        else:
            shutil.copy2(source, destination)
    return False

def copy_stage_base_image(source: str, destination: str, kernel: str | None, initramfs: str | None,
                          process_holder: list | None = None, cancel_event=None) -> bool:
    """Copy the immutable cached base to a writable run workspace; return whether root.img was cloned."""
    os.makedirs(destination, exist_ok=True)
    image_cloned = _clone_or_sparse_copy(os.path.join(source, "root.img"), os.path.join(destination, "root.img"),
                                         process_holder, cancel_event)
    files = ([kernel] if kernel else []) + ([initramfs] if initramfs else [])
    for name in files:
        _clone_or_sparse_copy(os.path.join(source, name), os.path.join(destination, name),
                              process_holder, cancel_event)
    return image_cloned

def create_stage_image_directory() -> str:
    """Creates an isolated VM image workspace in the selected Library's Temporary folder."""
    from .repository import Repository, resolve_path
    temporary = resolve_path(Repository.Settings.value.temporary_location)
    root = os.path.join(temporary, "TestVMImages")
    os.makedirs(root, exist_ok=True)
    return tempfile.mkdtemp(prefix="stage-", dir=root)

def cleanup_stage_image_directory(directory: str | None) -> None:
    """Removes only a VM image workspace created directly under Library/Temporary/TestVMImages."""
    if not directory:
        return
    try:
        from .repository import Repository, resolve_path
        temporary = resolve_path(Repository.Settings.value.temporary_location)
        root = os.path.realpath(os.path.join(temporary, "TestVMImages"))
        target = os.path.realpath(directory)
        if os.path.dirname(target) == root and os.path.basename(target).startswith("stage-"):
            shutil.rmtree(target, ignore_errors=True)
    except (AttributeError, OSError):
        pass

def stage_image_ready(support: QemuSupport) -> bool:
    if support.kind == QemuBuildKind.STAGE and not support.boot_kernel:
        return False
    directory = support.image_directory
    if not directory:
        return False
    if support.backend == "qemu" and not support.kernel_package and support.base_image_path:
        files = [support.base_image_path, support.base_kernel_path]
        if support.boot_initramfs:
            files.append(support.base_initramfs_path)
        return all(path and os.path.isfile(path) for path in files)
    files = ["root.img", support.boot_kernel] + ([support.boot_initramfs] if support.boot_initramfs else [])
    return all(os.path.isfile(os.path.join(directory, name)) for name in files)

def stage_extract_script(archive: str) -> str:
    """Stream the compressed archive to tar and report bytes consumed, then wait for tar."""
    import shlex
    from .ssh_connection import quote
    from .deploy_installation import tar_extract_command
    command = shlex.split(tar_extract_command(archive, "@ROOT@"))
    program = f'''
import os, subprocess, sys
archive, root = sys.argv[1:]
command = {command!r}
command = [root if value == "@ROOT@" else value for value in command]
size = os.path.getsize(archive) or 1
process = subprocess.Popen(command, stdin=subprocess.PIPE)
read = 0
reported = -1
try:
    with open(archive, "rb") as source:
        while chunk := source.read(1024 * 1024):
            process.stdin.write(chunk)
            read += len(chunk)
            percent = min(99, read * 100 // size)
            if percent != reported:
                print("@@extract-progress " + str(percent), flush=True)
                reported = percent
    process.stdin.close()
except BaseException:
    process.stdin.close()
    process.wait()
    raise
status = process.wait()
if status:
    sys.exit(status)
print("@@extract-progress 100", flush=True)
'''
    return f"python3 -c {quote(program)} {quote(archive)} \"$MNT\""


def stage_image_script(archive: str, directory: str, size: int, kernel: str | None, initramfs: str | None) -> str:
    """Creates ext4 disk image with stage (as root): root password removed (login without password, machine is for
    testing), serial console enabled, root filesystem in fstab. Kernel and initramfs are copied next to it."""
    from .ssh_connection import quote
    from .deploy_installation import tar_extract_command
    image = quote(os.path.join(directory, "root.img"))
    boot_files = " ".join(quote(name) for name in ([kernel] if kernel else []) +
                           ([initramfs] if initramfs else []))
    copy_boot_files = (f'for file in {boot_files}; do cp "$MNT/boot/$file" "$D/$file"; done'
                       if boot_files else ':')
    return f"""set -e
D={quote(directory)}
MNT=$(mktemp -d)
cleanup() {{ umount "$MNT" 2>/dev/null || true; rmdir "$MNT" 2>/dev/null || true; }}
trap cleanup EXIT
rm -f {image}
truncate -s {size} {image}
mkfs.ext4 -q -F -L root {image}
mount -o loop {image} "$MNT"
echo "Extracting stage"
{stage_extract_script(archive)}
echo "Configuring test system"
# Machine for testing: root logs in without password, on screen and serial console.
sed -i 's/^root:[^:]*:/root::/' "$MNT/etc/shadow"
echo "/dev/vda / ext4 noatime 0 1" > "$MNT/etc/fstab"
if [ -f "$MNT/etc/inittab" ]; then
    for entry in \
        "s0:12345:respawn:/sbin/agetty -L 115200 ttyS0 vt100" \
        "t0:12345:respawn:/sbin/agetty -L 115200 ttyAMA0 vt100" \
        "h0:12345:respawn:/sbin/agetty -L 115200 hvc0 vt100"; do
        id=${{entry%%:*}}
        grep -q "^$id:" "$MNT/etc/inittab" || echo "$entry" >> "$MNT/etc/inittab"
    done
fi
{copy_boot_files}
chmod a+r "$D"/*
echo "Disk image: $(du -h {image} | cut -f1) used of {size // (1024 ** 3)} GiB"
"""

def stage_image_mount_script(directory: str) -> str:
    """Mount the disposable guest root while a custom kernel is prepared."""
    from .ssh_connection import quote
    image, mount = quote(os.path.join(directory, "root.img")), quote(os.path.join(directory, "rootfs"))
    return f"set -e\nmkdir -p {mount}\nmount -o loop {image} {mount}\n"

def stage_image_unmount_script(directory: str) -> str:
    from .ssh_connection import quote
    mount = quote(os.path.join(directory, "rootfs"))
    return f"set -e\nif mountpoint -q {mount}; then umount {mount}; fi\nrmdir {mount} 2>/dev/null || true\n"

def stage_image_add_modules_script(directory: str) -> str:
    """Install modules built with a custom kernel into the temporary VM root filesystem."""
    from .ssh_connection import quote
    source = quote(os.path.join(directory, "custom-modules"))
    mount = quote(os.path.join(directory, "rootfs"))
    return f"set -e\nmkdir -p {mount}/lib/modules\ncp -a --no-preserve=ownership {source}/. {mount}/lib/modules/\n"

# ------------------------------------------------------------------------------
# Running machine.

class QemuRunEvent(Enum):
    STATE_CHANGED = auto()
    SERIAL_OUTPUT = auto()
    OUTPUT_LINE_ADDED = auto()
    RUNNING_RUNS_CHANGED = auto()

class QemuRun:
    """QEMU process of build, with sockets of display (VNC), serial console and monitor (QMP)."""

    running_runs: list[QemuRun] = []
    running_runs_event_bus = EventBus[QemuRunEvent]()
    _start_lock = threading.Lock()
    backend_name = "QEMU"

    def __init__(self, project_directory, build, support: QemuSupport, memory_mib: int = 4096, cpus: int = 4):
        self.project_directory = project_directory
        self.build = build
        self.support = support
        self.memory_mib = memory_mib
        self.cpus = cpus
        self.event_bus = EventBus[QemuRunEvent]()
        self.process: subprocess.Popen | None = None
        # Socket paths are short (limit of 104 characters on macOS).
        self.directory = _new_run_directory(self.backend_name)
        self.vnc_socket = os.path.join(self.directory, "vnc")
        self.serial_socket = os.path.join(self.directory, "serial")
        self.qmp_socket = os.path.join(self.directory, "qmp")
        self.ssh_port = _free_port() if self.backend_name == "QEMU" else None
        self.error: str | None = None
        self.output: list[str] = [] # QEMU own messages.
        self.serial_output = "" # Last MiB is retained while the view is closed and restored when reopened.
        self._serial: socket.socket | None = None

    @classmethod
    def running_for_build(cls, project_directory, build) -> QemuRun | None:
        project_id = getattr(project_directory, "id", None)
        artifact_path = getattr(build, "artifact_path", None)
        return next((run for run in cls.running_runs if run.is_running
                     and getattr(run.project_directory, "id", None) == project_id
                     and getattr(run.build, "artifact_path", None) == artifact_path), None)

    @property
    def name(self) -> str:
        return self.build.stage_name

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def accelerator(self) -> str | None:
        return accelerated(self.support.machine)

    def command(self) -> list[str]:
        machine = self.support.machine
        accelerator = self.accelerator
        binary = qemu_binary(machine.binary)
        if binary is None:
            raise RuntimeError(f"{machine.binary} is not installed")
        command = [binary, *machine.machine, "-m", str(self.memory_mib), "-smp", str(self.cpus),
                   "-name", self.build.stage_name]
        command += ["-accel", accelerator, "-cpu", "host"] if accelerator else ["-accel", "tcg", "-cpu", machine.cpu_emulated]
        if machine.firmware and (firmware := _firmware_path(machine.firmware)):
            command += ["-drive", f"if=pflash,format=raw,readonly=on,file={firmware}"]
        command += machine.display
        if machine.usb:
            command += ["-device", "qemu-xhci", "-device", "usb-kbd", "-device", "usb-tablet"]
        command += ["-netdev", f"user,id=net,hostfwd=tcp:127.0.0.1:{self.ssh_port}-:22",
                    "-device", f"virtio-net-{machine.device_bus},netdev=net"]
        command += ["-device", f"virtio-rng-{machine.device_bus}"]
        if self.support.kind == QemuBuildKind.ISO:
            command += ["-drive", f"file={self.build.artifact_path},format=raw,readonly=on,media=cdrom,if=none,id=cd",
                        "-device", "virtio-scsi-pci", "-device", "scsi-cd,drive=cd"]
        else:
            directory = self.support.image_directory
            if not directory:
                raise RuntimeError("Temporary VM image directory has not been allocated")
            if machine.disk_device:
                command += ["-drive", f"file={self.overlay},format=qcow2,if=none,id=disk",
                            "-device", f"{machine.disk_device},drive=disk"]
            else:
                command += ["-drive", f"file={self.overlay},format=qcow2,if={machine.disk_interface}"]
            command += ["-kernel", self.support.base_kernel_path or os.path.join(directory, self.support.boot_kernel),
                        "-append", f"root=/dev/vda rw rootwait console=tty0 console={machine.console}"]
            if self.support.boot_initramfs:
                command += ["-initrd", self.support.base_initramfs_path or
                            os.path.join(directory, self.support.boot_initramfs)]
        command += ["-pidfile", os.path.join(self.directory, "pid")] # Machines left by stopped app are found by it.
        command += ["-vnc", f"unix:{self.vnc_socket}", "-display", "none",
                    "-chardev", f"socket,id=serial,path={self.serial_socket},server=on,wait=off", "-serial", "chardev:serial",
                    "-qmp", f"unix:{self.qmp_socket},server=on,wait=off"]
        return command

    @property
    def overlay(self) -> str:
        return os.path.join(self.directory, "disk.qcow2")

    def start(self):
        with QemuRun._start_lock:
            if any(run.is_running for run in QemuRun.running_runs):
                raise RuntimeError("Only one QEMU machine can run at a time")
            if self.support.kind == QemuBuildKind.STAGE and not self.support.boot_kernel:
                raise RuntimeError("This stage has no boot kernel; build a custom kernel before running it")
            self._start_locked()

    def _start_locked(self):
        if self.support.kind == QemuBuildKind.STAGE:
            # Changes of system are discarded when machine stops: it runs on overlay of disk image.
            directory = self.support.image_directory
            if not self.support.base_image_path and not directory:
                raise RuntimeError("Temporary VM image directory has not been allocated")
            image = self.support.base_image_path or os.path.join(directory, "root.img")
            image_tool = qemu_binary("qemu-img")
            if image_tool is None:
                raise RuntimeError("qemu-img is not installed")
            subprocess.run([image_tool, "create", "-q", "-f", "qcow2", "-F", "raw", "-b", image, self.overlay],
                           check=True)
        command = self.command()
        self.output.append("$ " + " ".join(command))
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        QemuRun.running_runs.append(self)
        QemuRun.running_runs_event_bus.emit(QemuRunEvent.RUNNING_RUNS_CHANGED)
        threading.Thread(target=self._read_output, daemon=True).start()
        threading.Thread(target=self._read_serial, daemon=True).start()
        self.event_bus.emit(QemuRunEvent.STATE_CHANGED, self)

    def _read_output(self):
        for line in self.process.stdout:
            line = line.rstrip("\n")
            self.output.append(line)
            self.event_bus.emit(QemuRunEvent.OUTPUT_LINE_ADDED, line)
        code = self.process.wait()
        if code != 0 and not self.error:
            self.error = next((line for line in reversed(self.output) if line.strip()), f"QEMU exited with code {code}")
        if self in QemuRun.running_runs:
            QemuRun.running_runs.remove(self)
            QemuRun.running_runs_event_bus.emit(QemuRunEvent.RUNNING_RUNS_CHANGED)
        shutil.rmtree(self.directory, ignore_errors=True)
        cleanup_stage_image_directory(self.support.image_directory)
        self.event_bus.emit(QemuRunEvent.STATE_CHANGED, self)

    def _read_serial(self):
        connection = _connect_unix(self.serial_socket, lambda: self.is_running)
        if connection is None:
            return
        self._serial = connection
        while True:
            try:
                data = connection.recv(4096)
            except OSError:
                break
            if not data:
                break
            text = data.decode("utf-8", errors="replace")
            self.serial_output = (self.serial_output + text)[-1024 * 1024:]
            self.event_bus.emit(QemuRunEvent.SERIAL_OUTPUT, text)

    def send_serial(self, data: bytes):
        if self._serial is not None:
            try:
                self._serial.sendall(data)
            except OSError:
                pass

    def read_usage(self) -> tuple[float, int] | None:
        """Host CPU fraction of assigned vCPUs and QEMU resident memory in bytes."""
        if not self.is_running:
            return None
        try:
            ps = next((path for path in ("/bin/ps", "/usr/bin/ps")
                       if os.path.isfile(path) and os.access(path, os.X_OK)), None) or shutil.which("ps")
            if ps is None:
                return None
            result = subprocess.run([ps, "-p", str(self.process.pid), "-o", "%cpu=", "-o", "rss="],
                                    capture_output=True, text=True, check=True, timeout=3,
                                    env={**os.environ, "LC_ALL": "C"})
            fields = result.stdout.split()
            if len(fields) != 2:
                return None
            cpu_percent, resident_kib = fields
            cpu_fraction = float(cpu_percent) / (max(self.cpus, 1) * 100)
            return min(max(cpu_fraction, 0), 1), int(resident_kib) * 1024
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def read_disk_usage(self) -> tuple[int, int] | None:
        """Host storage allocated to the stage VM's writable overlay and its virtual disk capacity."""
        if self.support.kind != QemuBuildKind.STAGE or not self.is_running:
            return None
        if not self.support.image_directory:
            return None
        base_image = os.path.join(self.support.image_directory, "root.img")
        try:
            overlay = os.stat(self.overlay)
            base = os.stat(base_image)
            allocated_bytes = getattr(overlay, "st_blocks", 0) * 512 or overlay.st_size
            return allocated_bytes, base.st_size
        except OSError:
            return None

    # Monitor (QMP).

    def monitor(self, command: str, arguments: dict | None = None) -> dict | None:
        """Runs QMP command, None when machine doesn't answer."""
        try:
            return self._monitor(command, arguments)
        except (OSError, ValueError) as e:
            print(f"QEMU monitor command {command} failed: {e}")
            return None

    def _monitor(self, command: str, arguments: dict | None) -> dict | None:
        connection = _connect_unix(self.qmp_socket, lambda: self.is_running, attempts=5)
        if connection is None:
            return None
        with connection:
            reader = connection.makefile("r")
            reader.readline() # Greeting.
            for message in ({"execute": "qmp_capabilities"}, {"execute": command, **({"arguments": arguments} if arguments else {})}):
                connection.sendall((json.dumps(message) + "\n").encode())
                while True:
                    line = reader.readline()
                    if not line:
                        return None
                    response = json.loads(line)
                    if "return" in response or "error" in response:
                        break
            return response

    def power_off(self):
        """ACPI power button, system shuts down itself."""
        self.monitor("system_powerdown")

    def reset(self):
        self.monitor("system_reset")

    def send_ctrl_alt_delete(self):
        self.monitor("send-key", {"keys": [{"type": "qcode", "data": key} for key in ("ctrl", "alt", "delete")]})

    def stop(self):
        """Stops machine immediately: quit command of QEMU, process is terminated (and killed) when it doesn't end."""
        if not self.is_running:
            return
        process = self.process
        self.monitor("quit")
        def ensure_stopped():
            for signal_function in (None, process.terminate, process.kill):
                if signal_function:
                    signal_function()
                try:
                    process.wait(timeout=3)
                    return
                except subprocess.TimeoutExpired:
                    continue
        threading.Thread(target=ensure_stopped, daemon=True).start()

def stop_all_runs():
    """Stops machines of app (when it quits)."""
    for run in list(QemuRun.running_runs):
        try:
            run.stop()
        except Exception as e:
            print(f"Failed to stop QEMU: {e}")

def stop_orphaned_runs():
    """Stops VMs left by a killed app and removes their temporary run directories."""
    import glob, signal
    directories = set(glob.glob("/tmp/cl-qemu-*"))
    try:
        from .repository import Repository, resolve_path
        temporary = resolve_path(Repository.Settings.value.temporary_location)
        directories.update(glob.glob(os.path.join(temporary, "VMs", "cl-qemu-*")))
    except AttributeError:
        pass
    for directory in directories:
        try:
            with open(os.path.join(directory, "pid")) as file:
                pid = int(file.read().strip())
            if "qemu-system" in subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout:
                print(f"Stopping QEMU left by previous run ({pid})")
                os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        shutil.rmtree(directory, ignore_errors=True)
    try:
        from .repository import Repository, resolve_path
        temporary = resolve_path(Repository.Settings.value.temporary_location)
        image_root = os.path.join(temporary, "TestVMImages")
        for directory in glob.glob(os.path.join(image_root, "stage-*")):
            shutil.rmtree(directory, ignore_errors=True)
    except AttributeError:
        pass

def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]

def _connect_unix(path: str, alive, attempts: int = 100) -> socket.socket | None:
    """Connects to socket of QEMU, waiting until it's created."""
    import time
    for _ in range(attempts):
        if not alive():
            return None
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.connect(path)
            return connection
        except OSError:
            time.sleep(0.1)
    return None


# VM image and optional custom-kernel preparation.

def _kernel_output_cache_directory(build, architecture: str, package: str, snapshot, overlays, toolset=None) -> str:
    from .repository import Repository, resolve_path
    # Portage configuration and the resulting stage can differ between builds even when
    # they select the same kernel atom. Keep compiled outputs isolated by build identity.
    signature = [str(build.id), os.path.realpath(build.artifact_path or build.path),
                 architecture, package, snapshot.filename,
                 str(getattr(toolset, "uuid", None) or getattr(toolset, "name", ""))]
    for path in (build.artifact_path or build.path, snapshot.file_path()):
        try:
            stat = os.stat(path)
            signature.append(f"{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            signature.append("missing")
    for name, path in overlays:
        signature.append(f"overlay:{name}:{os.path.realpath(path)}")
        category = os.path.join(path, "sys-kernel")
        try:
            for root, directories, files in os.walk(category):
                for filename in sorted(files):
                    if filename.endswith(".ebuild"):
                        ebuild = os.path.join(root, filename)
                        stat = os.stat(ebuild)
                        signature.append(f"{ebuild}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            continue
    key = hashlib.sha256("\n".join(signature).encode()).hexdigest()[:24]
    return os.path.join(resolve_path(Repository.Settings.value.cache_location), "TestVMKernels", key)


def _kernel_outputs_ready(directory: str, package: str, require_arm64_image: bool = False) -> bool:
    modules = os.path.join(directory, "custom-modules")
    try:
        kernel = os.path.join(directory, "custom-kernel")
        with open(os.path.join(directory, KERNEL_OUTPUTS_MANIFEST), encoding="utf-8") as manifest:
            package_name, release = manifest.read().splitlines()
        if package_name != package.strip() or not release or os.path.basename(release) != release:
            return False
        if (not os.path.isfile(kernel)
                or (require_arm64_image and not is_arm64_linux_image(kernel))
                or not os.path.isfile(os.path.join(directory, "custom-initramfs"))
                or not os.path.isdir(modules)):
            return False
        with os.scandir(modules) as entries:
            module_trees = list(entries)
        return (len(module_trees) == 1 and module_trees[0].name == release
                and module_trees[0].is_dir(follow_symlinks=False))
    except (OSError, ValueError):
        return False



class QemuImagePreparation(MultiStageProcess):
    """Creates disk image of stage for QEMU (in virtual machine on macOS, as root on Linux)."""

    def __init__(self, project_directory, build, support):
        self.project_directory = project_directory
        self.project_id = project_directory.id if project_directory else None
        self.build = build
        self.support = support
        self.failure_reason: str | None = None
        self.canceled = False
        self.on_completed = None
        self._completion_notified = False
        self._canceling = False
        super().__init__(title="Start virtual machine")

    def setup_stages(self):
        self.stages.append(_StepImage("Prepare cached stage image", self, "cache"))
        self.stages.append(_StepImage("Prepare writable VM image", self, "image"))
        if self.support.kernel_package:
            self.stages.append(_StepImage("Build custom kernel", self, "kernel"))

    def name(self) -> str:
        return self.build.stage_name

    def complete_process(self, success: bool):
        if self._completion_notified:
            return
        self._completion_notified = True
        success = success and not self.canceled
        if not success:
            cleanup_stage_image_directory(self.support.image_directory)
        if self.on_completed:
            self.on_completed(success)

    def cancel(self):
        self.cancel_async()

    def cancel_async(self):
        if self._canceling or self.status not in (MultiStageProcessState.SETUP, MultiStageProcessState.IN_PROGRESS,
                                                   MultiStageProcessState.COMPLETED):
            return
        self.canceled = True
        self._canceling = True
        active = next((stage for stage in self.stages
                       if stage.state == MultiStageProcessStageState.IN_PROGRESS), None)
        if active is not None:
            active.request_cancel()

        def stop_work():
            if active is not None:
                active.cancel_work()
            GLib.idle_add(self._finish_cancel)
        threading.Thread(target=stop_work, daemon=True).start()

    def _finish_cancel(self):
        if self.status != MultiStageProcessState.FAILED:
            self.status = MultiStageProcessState.FAILED
            self.event_bus.emit(MultiStageProcessEvent.STATE_CHANGED, self.status)
        for stage in self.stages:
            if stage.state in (MultiStageProcessStageState.IN_PROGRESS, MultiStageProcessStageState.SCHEDULED):
                stage._update_state(MultiStageProcessStageState.FAILED)
        self._cleanup()
        self.clean_from_started_processes()
        self.complete_process(success=False)
        return False

class _StepImage(MultiStageProcessStage):
    def __init__(self, name, multistage_process, operation):
        descriptions = {
            "cache": "Extracts the stage into a reusable base image when needed",
            "image": "Creates this run's writable VM image",
            "kernel": "Builds and installs the selected kernel package",
        }
        super().__init__(name=name, description=descriptions[operation], multistage_process=multistage_process)
        self.operation = operation

    def log(self, line: str):
        if line.startswith("@@extract-progress "):
            try:
                percent = max(0, min(100, int(line.split()[-1])))
            except ValueError:
                return
            self._update_progress(percent / 100)
            line = f"Extracting stage · {percent}%"
        super().log(line)

    def start(self):
        if self.multistage_process.canceled:
            self._cancel_event.set()
            self._update_state(MultiStageProcessStageState.FAILED)
            return
        self.processes = []
        self._kernel_call = None
        self._worker_thread = threading.current_thread()
        super().start()
        process = self.multistage_process
        connection = None
        try:
            from .deploy_image import image_connection
            directory = process.support.image_directory
            if not directory:
                raise RuntimeError("Temporary VM image directory has not been allocated")
            os.makedirs(directory, exist_ok=True)
            connection = image_connection(process.project_directory)
            if error := connection.connect(process_holder=self.processes, output_handler=self.log,
                                           cancel_event=self._cancel_event):
                raise RuntimeError(error)
            self._check_canceled()
            if self.operation == "cache":
                self._prepare_cached_image(connection)
            elif self.operation == "image":
                self._prepare_writable_image()
            else:
                self._prepare_custom_kernel(connection)
            self._check_canceled()
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            if self._cancel_event.is_set():
                return
            self.multistage_process.failure_reason = str(e)
            self.log(f"Failed to prepare VM disk image: {e}")
            print(f"Failed to create QEMU image: {e}")
            self.complete(MultiStageProcessStageState.FAILED)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _cache_directory(self):
        build = self.multistage_process.build
        size = max(8 << 30, (os.path.getsize(build.artifact_path) * 5 // (1 << 30) + 4) << 30)
        return stage_image_cache_directory(build, self.multistage_process.support, size), size

    def _prepare_cached_image(self, connection):
        process = self.multistage_process
        build, support = process.build, process.support
        if not support.boot_kernel:
            raise RuntimeError("Stage has no boot kernel; select a custom kernel package to run it")
        cache_directory, size = self._cache_directory()
        if stage_base_image_ready(cache_directory, support.kernel, support.initramfs):
            self.log("Using cached base image")
            return
        shutil.rmtree(cache_directory, ignore_errors=True)
        os.makedirs(cache_directory, exist_ok=True)
        self.log("Extracting stage into reusable cache image")
        script = stage_image_script(build.artifact_path, cache_directory, size, support.kernel, support.initramfs)
        if not connection.run(script, self.log, self.processes):
            shutil.rmtree(cache_directory, ignore_errors=True)
            raise RuntimeError("Failed to create cached base disk image")
        Path(cache_directory, ".ready").write_text("ready\n", encoding="utf-8")

    def _prepare_writable_image(self):
        support = self.multistage_process.support
        cache_directory, _ = self._cache_directory()
        directory = support.image_directory
        if support.backend == "qemu" and not support.kernel_package:
            support.base_image_path = os.path.join(cache_directory, "root.img")
            support.base_kernel_path = os.path.join(cache_directory, support.kernel)
            support.base_initramfs_path = (os.path.join(cache_directory, support.initramfs)
                                           if support.initramfs else None)
            self.log("Using cached base image directly with a disposable QEMU overlay")
            return
        cloned = copy_stage_base_image(cache_directory, directory, support.kernel, support.initramfs,
                                       self.processes, self._cancel_event)
        self.log("Created copy-on-write clone of cached base image" if cloned else
                 "Copied cached base image; this filesystem does not support instant clones")

    def _prepare_custom_kernel(self, connection):
        directory = self.multistage_process.support.image_directory
        if not connection.run(stage_image_mount_script(directory), self.log, self.processes):
            raise RuntimeError("Failed to mount the temporary VM image for kernel preparation")
        self._check_canceled()
        try:
            self._emerge_kernel_package(directory)
            self._check_canceled()
            if not connection.run(stage_image_add_modules_script(directory), self.log, self.processes):
                raise RuntimeError("Failed to add custom kernel modules to the temporary VM image")
        finally:
            if not connection.run(stage_image_unmount_script(directory), self.log, self.processes):
                raise RuntimeError("Failed to unmount the temporary VM image")

    def _check_canceled(self):
        if self._cancel_event.is_set():
            raise RuntimeError("VM startup canceled")

    def cancel(self):
        if self.state != MultiStageProcessStageState.IN_PROGRESS:
            return
        self.request_cancel()
        self.cancel_work()

    def request_cancel(self):
        self._cancel_event.set()

    def cancel_work(self):
        call = getattr(self, "_kernel_call", None)
        if call is not None:
            if not getattr(self, "_kernel_cancel_sent", False):
                self._kernel_cancel_sent = True
                call.cancel()
        for running in getattr(self, "processes", []):
            try:
                if running.poll() is None:
                    running.terminate()
            except (AttributeError, OSError):
                continue
        worker = getattr(self, "_worker_thread", None)
        if worker and worker is not threading.current_thread():
            worker.join()

    def _emerge_kernel_package(self, directory: str):
        """Build a kernel in a disposable project-toolset overlay and export its boot files."""
        from .custom_kernel import kernel_package_build_command, portage_config_setup_command
        from .repository import Repository, resolve_path
        from .root_helper_server import ServerResponseStatusCode
        from .toolset import BindMount
        from .project_build_spec import generate_portage_confdir
        from .rootless import prepare_snapshot_squashfs, unmount_read_only_mount, distfiles_directory
        from .project_kernel_packages import _stage_overlays
        from .project_stage_arguments import StageArgumentDetails
        from .project_stage_value_resolver import resolve_stage_argument

        process = self.multistage_process
        project = process.project_directory
        snapshot = process.support.kernel_snapshot
        if snapshot is None:
            raise RuntimeError("Select a Gentoo snapshot to emerge a custom kernel")
        stage = next((item for item in project.stages if item.id == process.build.stage_id), None)
        toolset = process.support.kernel_toolset or project.get_toolset()
        if toolset is None:
            raise RuntimeError("A Gentoo toolset is required to build a custom kernel")
        overlays = _stage_overlays(project, stage) if stage is not None else []
        package_cache = None
        if stage is not None:
            package_cache = resolve_stage_argument(project, stage, StageArgumentDetails.pkgcache_path.value)
            if isinstance(package_cache, str) and package_cache.strip():
                package_cache = resolve_path(package_cache.strip())
            else:
                package_cache = None
        architecture = project.get_architecture()
        cache_directory = _kernel_output_cache_directory(
            process.build, architecture.value if architecture else "unknown",
            process.support.kernel_package, snapshot, overlays, toolset)
        require_arm64_image = (process.support.backend == "apple"
                               and process.support.machine.binary == "qemu-system-aarch64")
        if _kernel_outputs_ready(cache_directory, process.support.kernel_package, require_arm64_image):
            copy_kernel_outputs(cache_directory, directory)
            return

        if not toolset.reserve():
            raise RuntimeError(toolset.busy_reason or f"Toolset {toolset.name} is in use")

        # Mount the machine workspace before preparing Portage bindings. spawn acquires a second
        # workspace reference and releases it on unspawn.
        workspace_machine = toolset.machine
        workspace_user = f"Preparing VM kernel for {process.build.stage_name}"
        workspace_acquired = False
        temporary = resolve_path(Repository.Settings.value.temporary_location)
        os.makedirs(temporary, exist_ok=True)
        config_directory = tempfile.mkdtemp(prefix="test-vm-kernel-", dir=temporary)
        stage_config_directory = Path(config_directory, "stage-portage")
        stage_config_directory.mkdir()
        portage_directory = Path(config_directory, "merged-portage")
        portage_directory.mkdir()
        spawned = False
        snapshot_root = None
        snapshot_mounted = False
        os.makedirs(cache_directory, exist_ok=True)
        try:
            if workspace_machine is not None:
                workspace_machine.acquire_workspace(output_handler=self.log, user=workspace_user)
                workspace_acquired = True
            executor = toolset.executor
            snapshot_root, snapshot_mounted = prepare_snapshot_squashfs(
                snapshot.file_path(), self.log, executor=executor)

            if stage is not None:
                generate_portage_confdir(project, stage, str(stage_config_directory))
            repos_directory = stage_config_directory / "repos.conf"
            repos_directory.mkdir(parents=True, exist_ok=True)

            bindings = [
                BindMount(mount_path="/var/db/repos/gentoo", host_path=snapshot_root, read_only=True),
                BindMount(mount_path="/var/tmp/catalystlab-kernel-output", host_path=cache_directory,
                          store_changes=True, create_if_missing=True),
                BindMount(mount_path="/var/tmp/catalystlab-stage-portage", host_path=str(stage_config_directory),
                          read_only=True),
                BindMount(mount_path="/var/tmp/catalystlab-base-portage", toolset_path="/etc/portage",
                          read_only=True),
                # Keep the merged build configuration writable without relying on the
                # toolset's read-only /etc overlay to copy up all Portage config files.
                BindMount(mount_path="/etc/portage", host_path=str(portage_directory), store_changes=True),
                BindMount(mount_path="/var/cache/distfiles", host_path=distfiles_directory(executor),
                          store_changes=True, create_if_missing=True),
            ]
            use_binary_packages = False
            if package_cache:
                try:
                    info = executor.filesystem({"info": [package_cache]})["info"].get(package_cache, {})
                    if info.get("type") == "dir":
                        bindings.append(BindMount(mount_path="/var/cache/binpkgs", host_path=package_cache,
                                                  read_only=True))
                        use_binary_packages = True
                        self.log(f"Using build binary package cache read-only: {package_cache}")
                except Exception as error:
                    self.log(f"Build binary package cache is unavailable; packages will be built from source: {error}")
            repo_lines = ["[gentoo]", "location = /var/db/repos/gentoo", "auto-sync = no", ""]
            for overlay_name, overlay_path in overlays:
                overlay = next((item for item in Repository.OverlayDirectory.value
                                if item.directory_path() == overlay_path), None)
                repo_name = overlay.repo_name() if overlay else os.path.basename(overlay_path.rstrip(os.sep))
                if not repo_name or any(character.isspace() or character in "[]" for character in repo_name):
                    continue
                repo_lines.extend([f"[{repo_name}]", f"location = /var/db/repos/{repo_name}",
                                   "masters = gentoo", "auto-sync = no", ""])
                bindings.append(BindMount(mount_path=f"/var/db/repos/{repo_name}",
                                          host_path=overlay_path, read_only=True))
            repo_config = repos_directory / "99-catalystlab-test-vm.conf"
            repo_config.write_text("\n".join(repo_lines), encoding="utf-8")

            toolset.spawn(store_changes=False, additional_bindings=bindings, writable_root_overlay=True)
            if not toolset.spawned:
                raise RuntimeError("Failed to prepare a writable toolset root for the custom kernel")
            spawned = True
            pkgdir_config = ('printf \'%s\\n\' \'PKGDIR="/var/cache/binpkgs"\' >> /etc/portage/make.conf; '
                             if use_binary_packages else "")
            command = ("set -e; " + portage_config_setup_command() + pkgdir_config
                       + kernel_package_build_command(process.support.kernel_package, use_binary_packages,
                                                      require_arm64_image))
            response = []
            self._kernel_cancel_sent = False
            call = toolset.run_command(command=command, handler=self.log,
                                       completion_handler=response.append)
            self._kernel_call = call
            while call.thread and call.thread.is_alive():
                call.thread.join(timeout=0.1)
                if self._cancel_event.is_set() and not self._kernel_cancel_sent:
                    self._kernel_cancel_sent = True
                    call.cancel()
            self._kernel_call = None
            if not response or response[-1].code != ServerResponseStatusCode.OK:
                output = call.get_output() if hasattr(call, "get_output") else getattr(call, "output", [])
                details = "\n".join(output[-30:])
                suffix = f":\n{details}" if details else ""
                raise RuntimeError(f"Portage failed to emerge the custom kernel{suffix}")
        finally:
            try:
                if spawned:
                    toolset.unspawn(rebuild_squashfs_if_needed=False)
            finally:
                try:
                    if snapshot_mounted and snapshot_root:
                        unmount_read_only_mount(snapshot_root, self.log, executor=toolset.executor)
                finally:
                    try:
                        if workspace_acquired:
                            workspace_machine.release_workspace(output_handler=self.log, user=workspace_user)
                    finally:
                        toolset.release()
                        shutil.rmtree(config_directory, ignore_errors=True)
                        if not _kernel_outputs_ready(cache_directory, process.support.kernel_package,
                                                     require_arm64_image):
                            shutil.rmtree(cache_directory, ignore_errors=True)
        if not _kernel_outputs_ready(cache_directory, process.support.kernel_package, require_arm64_image):
            raise RuntimeError("Kernel package did not produce reusable boot files")
        copy_kernel_outputs(cache_directory, directory)
