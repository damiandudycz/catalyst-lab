from __future__ import annotations
import gzip, hashlib, json, os, re, shutil, socket, subprocess, sys, tempfile, threading, uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from .event_bus import EventBus

# ------------------------------------------------------------------------------
# Running builds in QEMU: live CD ISO images (booted by firmware of machine), and stages with generic kernel (stage
# kernel and initramfs started by QEMU, system in disk image made from stage archive). Display is served over VNC on
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
            from .repository import Repository
            temporary = os.path.realpath(os.path.expanduser(Repository.Settings.value.temporary_location))
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

    @property
    def available(self) -> bool:
        return self.reason is None

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
        if not kernels:
            return QemuSupport(kind, machine, reason="Stage has no kernel in /boot")
        if machine is None:
            return QemuSupport(kind, None, reason=f"Running {architecture.value if architecture else 'this architecture'} in QEMU isn't supported yet")
        # Kernel filenames are user-defined by Catalyst. Use the newest available kernel and let QEMU determine
        # whether it supports the selected virtual machine instead of guessing from target-specific names.
        kernel = sorted(kernels)[-1]
        version = re.sub(r"^(vmlinuz|vmlinux|Image)-?", "", kernel)
        initramfs = next((image for image in images if version and version in image), images[0] if images else None)
    else:
        return QemuSupport(None, machine, reason="Only stage3, stage4 and live CD builds can run")
    if machine is None:
        return QemuSupport(kind, None, reason=f"Running {architecture.value if architecture else 'this architecture'} in QEMU isn't supported yet")
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
# Disk image of stage (cached independently of build files).

def stage_image_directory(build, backend: str = "qemu") -> str:
    """Reusable test VM disk and boot files, shared by backends and cached by build artifact."""
    from .repository import Repository
    artifact = os.path.realpath(build.artifact_path or build.path)
    try:
        stat = os.stat(artifact)
        signature = f"{artifact}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        signature = artifact
    key = hashlib.sha256(signature.encode()).hexdigest()[:24]
    return os.path.join(Repository.Settings.value.cache_location, "TestVM", key)

def stage_image_ready(build, support: QemuSupport) -> bool:
    directory = stage_image_directory(build, support.backend)
    files = ["root.img", support.kernel] + ([support.initramfs] if support.initramfs else [])
    if not all(os.path.isfile(os.path.join(directory, name)) for name in files):
        # Move already prepared files from older releases out of the build folder once.
        legacy = os.path.join(build.path, support.backend)
        if all(os.path.isfile(os.path.join(legacy, name)) for name in files):
            os.makedirs(directory, exist_ok=True)
            for name in files:
                old, new = os.path.join(legacy, name), os.path.join(directory, name)
                if not os.path.exists(new):
                    shutil.move(old, new)
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


def stage_image_script(archive: str, directory: str, size: int, kernel: str, initramfs: str | None) -> str:
    """Creates ext4 disk image with stage (as root): root password removed (login without password, machine is for
    testing), serial console enabled, root filesystem in fstab. Kernel and initramfs are copied next to it."""
    from .ssh_connection import quote
    from .deploy_installation import tar_extract_command
    image = quote(os.path.join(directory, "root.img"))
    boot_files = " ".join(quote(name) for name in [kernel] + ([initramfs] if initramfs else []))
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
for file in {boot_files}; do cp "$MNT/boot/$file" "$D/$file"; done
chmod a+r "$D"/*
echo "Disk image: $(du -h {image} | cut -f1) used of {size // (1024 ** 3)} GiB"
"""

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
            directory = stage_image_directory(self.build)
            if machine.disk_device:
                command += ["-drive", f"file={self.overlay},format=qcow2,if=none,id=disk",
                            "-device", f"{machine.disk_device},drive=disk"]
            else:
                command += ["-drive", f"file={self.overlay},format=qcow2,if={machine.disk_interface}"]
            command += ["-kernel", os.path.join(directory, self.support.kernel),
                        "-append", f"root=/dev/vda rw rootwait console=tty0 console={machine.console}"]
            if self.support.initramfs:
                command += ["-initrd", os.path.join(directory, self.support.initramfs)]
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
            self._start_locked()

    def _start_locked(self):
        if self.support.kind == QemuBuildKind.STAGE:
            # Changes of system are discarded when machine stops: it runs on overlay of disk image.
            image = os.path.join(stage_image_directory(self.build), "root.img")
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
        base_image = os.path.join(stage_image_directory(self.build), "root.img")
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
        from .repository import Repository
        temporary = os.path.realpath(os.path.expanduser(Repository.Settings.value.temporary_location))
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
