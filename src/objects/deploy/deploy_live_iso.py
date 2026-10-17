from __future__ import annotations
import hashlib, os, re, shutil, subprocess, sys, tarfile, urllib.request
from dataclasses import dataclass
from enum import Enum
from datetime import datetime
from .multistage_process import MultiStageProcess, MultiStageProcessEvent
from .ssh_connection import quote
from .deploy_log import write_log_line
from .deploy_step import DeployStep

# ------------------------------------------------------------------------------
# Deploying live CD builds (ISO images): written to disk of this computer (USB drive, SD card), or saved as new ISO
# file. ISO is used as it is, or prepared for Raspberry Pi firmware when it has Raspberry Pi kernel.
#
# Raspberry Pi firmware doesn't boot ISO images (GRUB on EFI partition): it reads its configuration (config.txt),
# kernel, initramfs and device trees from FAT partition. New ISO is made from ISO built by catalyst: the same ISO 9660
# contents (live system squashfs, kernel, GRUB), with FAT partition for Raspberry Pi as first MBR partition and EFI
# partition (image of original ISO) as second one, so it still boots on UEFI machines. It's written to USB drive or SD
# card as it is (dd, Raspberry Pi Imager). Kernel started by Raspberry Pi firmware finds live system like when booted
# by GRUB: initramfs (genkernel cdroot) searches disks for ISO with livecd file and mounts squashfs from it.
#
# Device trees, overlays and firmware files of older models come from Raspberry Pi firmware repository, from release
# of the kernel built for ISO (raspberrypi-sources and firmware releases have the same date), verified with Manifest of
# Gentoo repository. They are downloaded once and kept in Cache of library.

RASPBERRY_PI_ISO_SUFFIX = "-raspberry-pi"
_FIRMWARE_ARCHIVE_URL = "https://github.com/raspberrypi/firmware/archive/{tag}.tar.gz"
_FIRMWARE_PACKAGES = ("sys-boot/raspberrypi-firmware", "sys-kernel/raspberrypi-image") # Their Manifests list archives.
_KERNEL_RELEASE = re.compile(r"_p(\d{8})-raspberrypi")
_REQUIRED_TOOLS = ("xorriso", "mkfs.vfat", "mcopy")
_RASPBERRY_PI_ARCHITECTURES = ("arm", "arm64")

def is_live_iso_build(build) -> bool:
    """Completed build of live CD with its ISO image."""
    from .project_build import StageBuildStatus
    return (build.status == StageBuildStatus.COMPLETED and bool(build.artifact) and build.artifact.endswith(".iso")
            and os.path.isfile(build.artifact_path or ""))

def raspberry_pi_kernels(build) -> list[str]:
    """Kernels of ISO built from Raspberry Pi kernel sources (from spec of build)."""
    spec = os.path.join(build.path or "", "stage.spec")
    try:
        with open(spec, encoding="utf-8") as file:
            text = file.read()
    except OSError:
        return []
    return re.findall(r"^boot/kernel/([^/\s]+)/sources:\s*\S*raspberrypi\S*", text, re.MULTILINE)

def supports_raspberry_pi_iso(project_directory, build) -> bool:
    """ISO with Raspberry Pi kernel, built for ARM: it can be prepared for Raspberry Pi firmware."""
    architecture = project_directory.get_architecture() if project_directory else None
    return (is_live_iso_build(build) and architecture is not None and architecture.value in _RASPBERRY_PI_ARCHITECTURES
            and bool(raspberry_pi_kernels(build)))

def live_iso_kernel_warning(build) -> str | None:
    """Warning about kernels of ISO built by genkernel (from spec of build) whose configuration in ISO doesn't build
    ISO 9660 into kernel: live system isn't found when it boots. None when it's fine or can't be checked."""
    from .project_stage_kernels import kernel_config_iso9660_warning
    try:
        with open(os.path.join(build.path or "", "stage.spec"), encoding="utf-8") as file:
            spec = file.read()
    except OSError:
        return None
    names = (re.search(r"^boot/kernel:\s*(.*)$", spec, re.MULTILINE) or [None, ""])[1].split()
    tool = shutil.which("bsdtar")
    for name in names:
        if re.search(rf"^boot/kernel/{re.escape(name)}/distkernel:\s*yes", spec, re.MULTILINE):
            continue # Initramfs made by dracut.
        if tool is None:
            return None
        result = subprocess.run([tool, "-xOf", build.artifact_path, f"boot/{name}-config"], capture_output=True, text=True, errors="replace")
        if result.returncode == 0 and result.stdout and (warning := kernel_config_iso9660_warning(result.stdout)):
            return f"Kernel {name}: {warning}. Rebuild live CD with ISO 9660 built into kernel."
    return None

def raspberry_pi_iso_path(build) -> str:
    """ISO prepared for Raspberry Pi, next to ISO of build."""
    return os.path.join(build.path, build.artifact.removesuffix(".iso") + RASPBERRY_PI_ISO_SUFFIX + ".iso")

def _firmware_cache_directory(tag: str) -> str:
    from .repository import library_directory
    return os.path.join(library_directory(), "Cache", "raspberrypi-firmware", tag)

# ------------------------------------------------------------------------------
# Running tools: as user on Linux (only files are created), in virtual machine (as root) on other systems.

class _UserConnection:
    host = "this computer"

    def connect(self) -> str | None:
        return None

    def close(self):
        pass

    def is_alive(self) -> bool:
        return True

    def run(self, script: str, output_handler=print, process_holder: list | None = None) -> bool:
        process = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if process_holder is not None:
            process_holder.append(process)
        for line in process.stdout:
            output_handler(line.rstrip("\n"))
        return process.wait() == 0

def _connection(project_directory, root: bool):
    """Runs tools preparing ISO. Live system is unpacked as root (owners of its files are kept), in working space of
    virtual machine on other systems than Linux."""
    if sys.platform.startswith("linux"):
        if root:
            from .local_disk_connection import LocalDiskConnection
            return LocalDiskConnection()
        return _UserConnection()
    from .deploy_image import MachineRootConnection, image_machine
    return MachineRootConnection(image_machine(project_directory))

# ------------------------------------------------------------------------------
# Boot configuration.

def parse_grub_entry(grub_cfg: str) -> tuple[str, str, str | None]:
    """Kernel, its arguments and initramfs of first menu entry of GRUB configuration of ISO."""
    entry = grub_cfg.split("menuentry", 1)[1] if "menuentry" in grub_cfg else grub_cfg
    linux = re.search(r"^\s*linux\s+(\S+)(.*)$", entry, re.MULTILINE)
    if not linux:
        raise RuntimeError("Kernel not found in GRUB configuration of ISO")
    initrd = re.search(r"^\s*initrd\s+(\S+)", entry, re.MULTILINE)
    return linux.group(1), linux.group(2).strip(), initrd.group(1) if initrd else None

@dataclass
class RaspberryPiBootSettings:
    """Boot configuration of ISO prepared for Raspberry Pi firmware."""
    serial_console: bool = True # Console on serial port (GPIO 14/15, debug connector of Pi 5) too.
    extra_arguments: str = "" # Added to kernel arguments.
    # Added to config.txt. Graphics driver (KMS, firmware uses version for Pi 5 itself) is needed by desktops.
    config_lines: str = "dtparam=audio=on\ndtoverlay=vc4-kms-v3d\nmax_framebuffers=2\ndisable_overscan=1"
    older_models_firmware: bool = True # Firmware files of models before Pi 5 (Pi 5 has it in EEPROM).

def raspberry_pi_config(kernel: str, initramfs: str | None, settings: RaspberryPiBootSettings) -> str:
    lines = [
        "# Created by Catalyst Lab: starts live system of this ISO.",
        "# See https://www.raspberrypi.com/documentation/computers/config_txt.html",
        "arm_64bit=1",
        f"kernel={kernel}",
    ]
    if initramfs:
        lines.append(f"initramfs {initramfs} followkernel")
    lines += [line.strip() for line in settings.config_lines.splitlines() if line.strip()]
    return "\n".join(lines) + "\n"

def raspberry_pi_cmdline(arguments: str, settings: RaspberryPiBootSettings) -> str:
    """Kernel arguments from GRUB, with consoles, root-device waiting and own arguments."""
    words = arguments.split()
    extra = []
    if not any(word.startswith("console=") for word in words):
        extra += (["console=serial0,115200"] if settings.serial_console else []) + ["console=tty1"]
    if "rootwait" not in words + settings.extra_arguments.split():
        extra.append("rootwait")
    return " ".join(extra + words + settings.extra_arguments.split()) + "\n"

# ------------------------------------------------------------------------------
# Process.

class LiveIsoTarget(Enum):
    ISO_FILE = "iso-file" # New ISO next to build.
    LOCAL_DISK = "local-disk" # Disk of this computer, written with ISO.

class LiveIsoDeployment(MultiStageProcess):
    """Deploys live CD build: ISO is prepared for Raspberry Pi (when raspberry_pi settings are given) and written to
    disk of this computer (connection running scripts as root) or saved as new ISO file."""

    def __init__(self, project_directory, build, target: LiveIsoTarget, disk=None, disk_connection=None,
                 raspberry_pi: RaspberryPiBootSettings | None = None, output_path: str | None = None, system=None):
        self.project_directory = project_directory
        self.project_id = project_directory.id if project_directory else None
        self.build = build
        self.target = target
        self.disk = disk # TargetDisk, for LOCAL_DISK.
        self.disk_connection = disk_connection
        self.raspberry_pi = raspberry_pi
        self.system = system # DeploySystemSettings applied to live system (repacked), None keeps it unchanged.
        self.iso_path = build.artifact_path
        self.live_directory = None # Live system unpacked and packed again, where tools run.
        self._workspace_machine = None
        self.work_directory = os.path.join(build.path, ".live-iso-deploy")
        # ISO file selected by user (ISO_FILE). ISO is prepared in build folder (shared with virtual machine), then
        # moved there.
        self.output_path = output_path
        if not self.modified:
            self.prepared_iso = self.iso_path # Written as it is.
        else:
            self.prepared_iso = os.path.join(self.work_directory, os.path.basename(raspberry_pi_iso_path(build)))
        self.connection = None # Runs tools preparing ISO.
        self.kernel_name = None # Files in ISO, from GRUB configuration.
        self.kernel_arguments = ""
        self.initramfs_name = None
        self.firmware_directory = None
        self.log_path = os.path.join(build.path, f"deploy-{datetime.now().strftime('%Y%m%dT%H%M%S')}-live-cd.log")
        self._log_file = None
        super().__init__(title="Deploy Live CD")
        self.event_bus.subscribe(MultiStageProcessEvent.STATE_CHANGED, self._notify_project)

    # Status of project in Deploy section follows deployment (running, result until it's seen).
    def _notify_project(self, *args):
        from .repository import Repository
        from .event_bus import SharedEvent
        project = next((item for item in Repository.ProjectDirectory.value if item.id == self.project_id), None)
        if project is not None:
            project.event_bus.emit(SharedEvent.STATE_UPDATED, project)

    def on_result_seen(self):
        self._notify_project()

    @property
    def modified(self) -> bool:
        """New ISO is made (boot partition for Raspberry Pi, or configured live system)."""
        return self.raspberry_pi is not None or self.system is not None

    def setup_stages(self):
        if self.modified:
            self.stages.append(_StepPrepare(multistage_process=self))
        if self.raspberry_pi is not None:
            self.stages.append(_StepReadIso(multistage_process=self))
            self.stages.append(_StepFirmware(multistage_process=self))
            self.stages.append(_StepBootPartition(multistage_process=self))
        if self.system is not None:
            self.stages.append(_StepLiveSystem(multistage_process=self))
        if self.modified:
            self.stages.append(_StepWriteIso(multistage_process=self))
        if self.target == LiveIsoTarget.LOCAL_DISK:
            self.stages.append(_StepWriteDisk(multistage_process=self))
        else:
            self.stages.append(_StepSaveIso(multistage_process=self))

    @property
    def target_name(self) -> str:
        if self.target == LiveIsoTarget.LOCAL_DISK:
            return self.disk.path if self.disk else "disk"
        return os.path.basename(self.output_path or self.prepared_iso)

    @property
    def result_path(self) -> str | None:
        """Created ISO file."""
        return self.output_path if self.target == LiveIsoTarget.ISO_FILE else None

    def name(self) -> str:
        return f"{self.build.stage_name} on {self.target_name}"

    def write_log(self, line: str):
        self._log_file = write_log_line(self.log_path, self._log_file, line)

    def complete_process(self, success: bool):
        if self.live_directory and self.connection:
            try:
                self.connection.run(f"umount -R -l {quote(self.live_directory)}/root/* 2>/dev/null; rm -rf {quote(self.live_directory)}", self.write_log)
            except Exception as e:
                print(f"Failed to remove live system: {e}")
        if self._workspace_machine:
            self._workspace_machine.release_workspace(user="Deploy Live CD")
            self._workspace_machine = None
        shutil.rmtree(self.work_directory, ignore_errors=True)
        if not success and self.output_path:
            try:
                os.remove(self.output_path + ".part")
            except OSError:
                pass
        self.write_log(f"=== Deployment {'completed' if success else 'failed'}" + (f": {self.result_path}" if success and self.result_path else ""))
        if self._log_file:
            self._log_file.close()
            self._log_file = None
        for connection in (self.connection, self.disk_connection):
            if connection:
                try:
                    connection.close()
                except Exception as e:
                    print(f"Failed to close connection: {e}")

class _StepPrepare(DeployStep):
    def __init__(self, multistage_process):
        super().__init__(name="Prepare", description="Checks tools that make new ISO", multistage_process=multistage_process)

    def run(self):
        process = self.multistage_process
        if not os.path.isfile(process.iso_path):
            raise RuntimeError("ISO of build doesn't exist anymore")
        process.connection = _connection(process.project_directory, root=process.system is not None)
        if error := process.connection.connect():
            raise RuntimeError(error)
        tools = ["xorriso"] + (["mkfs.vfat", "mcopy"] if process.raspberry_pi is not None else []) \
            + (["unsquashfs", "mksquashfs", "chroot"] if process.system is not None else [])
        self.remote(f"""
missing=""
for tool in {" ".join(tools)}; do command -v "$tool" > /dev/null || missing="$missing $tool"; done
[ -z "$missing" ] || {{ echo "Missing tools:$missing (packages xorriso, dosfstools, mtools and squashfs-tools)"; exit 1; }}
""", "Tools needed to create ISO are missing")
        shutil.rmtree(process.work_directory, ignore_errors=True)
        os.makedirs(process.work_directory)
        if process.system is not None:
            # Live system is unpacked in working space of virtual machine, or in /var/tmp (own filesystem keeps
            # owners and special files).
            import uuid
            machine = getattr(process.connection, "machine", None)
            if machine is not None:
                from .lima import MACHINE_DATA_DIRECTORY
                machine.acquire_workspace(self.log, self.processes, user="Deploy Live CD")
                process._workspace_machine = machine
                process.live_directory = f"{MACHINE_DATA_DIRECTORY}/Temporary/live-iso-{uuid.uuid4().hex[:8]}"
            else:
                process.live_directory = f"/var/tmp/catalystlab-live-iso-{uuid.uuid4().hex[:8]}"

# Changes of live CD (Gentoo livecd-tools): root password is scrambled at boot by pwgen service, unless it's set
# (pwgen does nothing then). SSH server is started by autoconfig when enabled in its configuration.
_LIVE_PWGEN = """#!/sbin/openrc-run
# Root password was set by Catalyst Lab, it's not scrambled.
depend() {
	before local
}
start() {
	einfo "Root password set by Catalyst Lab is kept"
}
"""

def live_system_script(live_directory: str, iso_path: str, system) -> str:
    """Unpacks live system of ISO, configures it like installed system and packs it again (same compression)."""
    from .deploy_boot import CHROOT_SETUP
    from .deploy_installation import system_configuration_script
    from .deploy_system import network_script, NetworkService
    import base64
    live = quote(live_directory)
    lines = ["set -e", f"L={live}", 'rm -rf "$L"; mkdir -p "$L"',
             f'xorriso -osirrox on -indev {quote(iso_path)} -extract /image.squashfs "$L/original.squashfs" 2>&1 | grep -v "^xorriso : UPDATE" || true',
             'test -s "$L/original.squashfs" || { echo "ISO has no live system (image.squashfs)"; exit 1; }',
             'INFO=$(unsquashfs -s "$L/original.squashfs")',
             'COMPRESSION=$(echo "$INFO" | awk \'/^Compression/ { print $2 }\')',
             'BLOCK=$(echo "$INFO" | awk \'/^Block size/ { print $3 }\')',
             'echo "Unpacking live system (${COMPRESSION:-gzip} compression)"',
             'unsquashfs -no-progress -d "$L/root" "$L/original.squashfs" > /dev/null',
             'rm -f "$L/original.squashfs"',
             'ROOT="$L/root"',
             # Name resolution of this machine is used in chroot, live system gets its own back.
             '[ -e "$ROOT/etc/resolv.conf" ] || [ -L "$ROOT/etc/resolv.conf" ] && mv "$ROOT/etc/resolv.conf" "$L/resolv.conf"',
             CHROOT_SETUP]
    lines += system_configuration_script(system)
    if system.network.service != NetworkService.NONE:
        lines.append(network_script(system.network))
    if system.wifi is not None:
        from .deploy_system import wifi_script
        lines.append(wifi_script(system.wifi))
    if system.root_password:
        encoded = base64.b64encode(_LIVE_PWGEN.encode()).decode()
        lines.append(f'[ -e "$ROOT/etc/init.d/pwgen" ] && echo {encoded} | base64 -d > "$ROOT/etc/init.d/pwgen" && echo "Root password is not scrambled at boot"')
    if system.enable_ssh:
        lines.append('[ -f "$ROOT/etc/conf.d/autoconfig" ] && sed -i \'s/^SSHD=.*/SSHD="yes"/\' "$ROOT/etc/conf.d/autoconfig" && echo "SSH server started at boot"')
    if system.localization.keymap:
        lines.append('[ -e "$ROOT/etc/init.d/keymaps" ] && [ -d "$ROOT/etc/runlevels/boot" ] && ln -sf /etc/init.d/keymaps "$ROOT/etc/runlevels/boot/keymaps"')
    lines += [
        'umount -l "$ROOT/run" "$ROOT/dev" "$ROOT/sys" "$ROOT/proc" 2>/dev/null || true', "trap - EXIT",
        'rm -f "$ROOT/etc/resolv.conf"; [ -e "$L/resolv.conf" ] || [ -L "$L/resolv.conf" ] && mv "$L/resolv.conf" "$ROOT/etc/resolv.conf"',
        'echo "Packing live system"',
        'mksquashfs "$ROOT" "$L/image.squashfs" -noappend -no-progress ${COMPRESSION:+-comp "$COMPRESSION"} ${BLOCK:+-b "$BLOCK"} > /dev/null',
        'rm -rf "$ROOT"',
        'echo "Live system: $(du -m "$L/image.squashfs" | cut -f1) MiB"',
    ]
    return "\n".join(lines) + "\n"

class _StepLiveSystem(DeployStep):
    def __init__(self, multistage_process):
        super().__init__(name="Configure live system", description="Unpacks live system, applies settings and packs it again",
                         multistage_process=multistage_process)

    def run(self):
        process = self.multistage_process
        self.remote(live_system_script(process.live_directory, process.iso_path, process.system), "Failed to configure live system")

class _StepReadIso(DeployStep):
    def __init__(self, multistage_process):
        super().__init__(name="Read ISO", description="Reads kernel, its arguments and EFI partition of ISO",
                         multistage_process=multistage_process)

    def run(self):
        process = self.multistage_process
        if not os.path.isfile(process.iso_path):
            raise RuntimeError("ISO of build doesn't exist anymore")
        work, iso = quote(process.work_directory), quote(process.iso_path)
        self.remote(f"""set -e
xorriso -osirrox on -indev {iso} -extract /boot/grub/grub.cfg {work}/grub.cfg -extract /efi.img {work}/efi.img 2>&1 | grep -v '^xorriso : UPDATE' || true
test -f {work}/grub.cfg || {{ echo "ISO has no GRUB configuration (boot/grub/grub.cfg)"; exit 1; }}
test -f {work}/efi.img || {{ echo "ISO has no EFI image (efi.img)"; exit 1; }}
chmod -R u+rw {work}
""", "Failed to read ISO")
        with open(os.path.join(process.work_directory, "grub.cfg"), encoding="utf-8") as file:
            kernel, arguments, initramfs = parse_grub_entry(file.read())
        process.kernel_name, process.kernel_arguments, process.initramfs_name = kernel.lstrip("/"), arguments, (initramfs or "").lstrip("/") or None
        self.log(f"Kernel: {process.kernel_name}, initramfs: {process.initramfs_name}, arguments: {arguments}")
        # Kernel, initramfs and kernel configuration (it names kernel release) are copied to boot partition.
        boot = os.path.join(process.work_directory, "boot")
        os.makedirs(boot)
        names = [process.kernel_name] + ([process.initramfs_name] if process.initramfs_name else [])
        extracts = " ".join(f"-extract /{quote(name)} {quote(os.path.join(boot, os.path.basename(name)))}" for name in names)
        config = process.kernel_name + "-config"
        self.remote(f"""set -e
xorriso -osirrox on -indev {iso} {extracts} -extract /{quote(config)} {work}/kernel-config 2>&1 | grep -v '^xorriso : UPDATE' || true
chmod -R u+rw {work}
""", "Failed to read kernel of ISO")
        for name in names:
            if not os.path.isfile(os.path.join(boot, os.path.basename(name))):
                raise RuntimeError(f"{name} is missing in ISO")

class _StepFirmware(DeployStep):
    def __init__(self, multistage_process):
        super().__init__(name="Raspberry Pi boot files", description="Downloads device trees and firmware of kernel release",
                         multistage_process=multistage_process)

    def run(self):
        process = self.multistage_process
        tag = self._firmware_tag()
        directory = _firmware_cache_directory(tag)
        if os.path.isdir(os.path.join(directory, "boot")):
            self.log(f"Using boot files of firmware {tag} from cache")
        else:
            self._download(tag, directory)
        process.firmware_directory = os.path.join(directory, "boot")

    def _firmware_tag(self) -> str:
        """Release of firmware matching kernel of ISO (date of raspberrypi-sources release), newest release in Gentoo
        repository otherwise."""
        process = self.multistage_process
        try:
            with open(os.path.join(process.work_directory, "kernel-config"), encoding="utf-8") as file:
                header = file.read(4096)
            if match := _KERNEL_RELEASE.search(header):
                self.log(f"Kernel release {match.group(1)}")
                return f"1.{match.group(1)}"
        except OSError:
            pass
        tags = sorted(self._manifest_hashes())
        if not tags:
            raise RuntimeError("Release of kernel is not known and Gentoo repository has no Raspberry Pi firmware")
        self.log(f"Kernel release is not known, using newest firmware {tags[-1]}")
        return tags[-1]

    def _snapshot_path(self) -> str | None:
        """Snapshot used by build, or snapshot of project."""
        from .repository import Repository
        process = self.multistage_process
        name = (((process.build.details or {}).get("inputs") or {}).get("snapshot") or {}).get("file")
        snapshots = list(Repository.Snapshot.value)
        snapshot = next((item for item in snapshots if item.filename == name), None)
        snapshot = snapshot or (process.project_directory.get_snapshot() if process.project_directory else None)
        snapshot = snapshot or (max(snapshots, key=lambda item: item.date or 0) if snapshots else None)
        return snapshot.file_path() if snapshot else None

    def _manifest_hashes(self) -> dict[str, str]:
        """SHA512 of firmware archives by release, from Manifests of Gentoo repository."""
        hashes = {}
        snapshot = self._snapshot_path()
        if not snapshot:
            return hashes
        for package in _FIRMWARE_PACKAGES:
            result = subprocess.run(["unsquashfs", "-cat", snapshot, f"{package}/Manifest"], capture_output=True, text=True)
            for match in re.finditer(r"^DIST raspberrypi-firmware-(\S+)\.tar\.gz \d+ .*?SHA512 ([0-9a-f]+)", result.stdout, re.MULTILINE):
                hashes[match.group(1)] = match.group(2)
        return hashes

    def _download(self, tag: str, directory: str):
        expected = self._manifest_hashes().get(tag)
        url = _FIRMWARE_ARCHIVE_URL.format(tag=tag)
        self.log(f"Downloading {url}")
        os.makedirs(os.path.dirname(directory), exist_ok=True)
        archive = directory + ".tar.gz.part"
        digest = hashlib.sha512()
        try:
            with urllib.request.urlopen(url, timeout=60) as response, open(archive, "wb") as file:
                size = int(response.headers.get("Content-Length") or 0)
                received, reported = 0, -1
                while chunk := response.read(1024 * 1024):
                    if self._cancel_event.is_set():
                        raise RuntimeError("Canceled")
                    file.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if size:
                        self._update_progress(received / size * 0.9)
                    elif (megabytes := received // (20 * 1024 * 1024)) != reported:
                        reported = megabytes
                        self.log(f"{received // (1024 * 1024)} MiB")
            if expected:
                if digest.hexdigest() != expected:
                    raise RuntimeError("Downloaded archive doesn't match checksum in Gentoo repository")
                self.log("Checksum matches Manifest of Gentoo repository")
            else:
                self.log(f"Warning: firmware {tag} is not in Gentoo repository, archive is not verified")
            # Only boot files are kept: device trees, overlays and firmware of models that need it (not kernels).
            temporary = directory + ".part"
            shutil.rmtree(temporary, ignore_errors=True)
            with tarfile.open(archive, "r:gz") as tar:
                for member in tar:
                    parts = member.name.split("/")
                    if len(parts) < 3 or parts[1] != "boot" or not (member.isfile() or member.isdir()):
                        continue
                    relative = "/".join(parts[2:])
                    if not relative or (len(parts) == 3 and re.match(r"kernel.*\.img$", parts[2])):
                        continue
                    target = os.path.join(temporary, "boot", relative)
                    if member.isdir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with tar.extractfile(member) as source, open(target, "wb") as output:
                        shutil.copyfileobj(source, output)
            if not any(name.endswith(".dtb") for name in os.listdir(os.path.join(temporary, "boot"))):
                raise RuntimeError("Archive has no device trees")
            shutil.rmtree(directory, ignore_errors=True)
            os.rename(temporary, directory)
        finally:
            try:
                os.remove(archive)
            except OSError:
                pass

class _StepBootPartition(DeployStep):
    def __init__(self, multistage_process):
        super().__init__(name="Boot partition", description="Creates FAT partition read by Raspberry Pi firmware",
                         multistage_process=multistage_process)

    def run(self):
        process = self.multistage_process
        boot = os.path.join(process.work_directory, "boot")
        # Firmware files and device trees, then kernel files of ISO.
        for name in os.listdir(process.firmware_directory):
            if not process.raspberry_pi.older_models_firmware and re.match(r"(bootcode\.bin|start.*\.elf|fixup.*\.dat)$", name):
                continue # Pi 5 has its firmware in EEPROM.
            source = os.path.join(process.firmware_directory, name)
            if os.path.isdir(source):
                shutil.copytree(source, os.path.join(boot, name))
            else:
                shutil.copy2(source, boot)
        kernel = os.path.basename(process.kernel_name)
        initramfs = os.path.basename(process.initramfs_name) if process.initramfs_name else None
        with open(os.path.join(boot, "config.txt"), "w", encoding="utf-8") as file:
            file.write(raspberry_pi_config(kernel, initramfs, process.raspberry_pi))
        with open(os.path.join(boot, "cmdline.txt"), "w", encoding="utf-8") as file:
            file.write(raspberry_pi_cmdline(process.kernel_arguments, process.raspberry_pi))
        self.log("config.txt:")
        for line in raspberry_pi_config(kernel, initramfs, process.raspberry_pi).splitlines():
            self.log(f"  {line}")
        self.log(f"cmdline.txt: {raspberry_pi_cmdline(process.kernel_arguments, process.raspberry_pi).strip()}")
        size = sum(os.path.getsize(os.path.join(root, name)) for root, _, names in os.walk(boot) for name in names)
        # FAT32 with 512 byte clusters: files fit with little waste, enough clusters for FAT32 from 64 MiB.
        kib = max(64 * 1024, (size * 12 // 10) // 1024 + 16 * 1024)
        image = os.path.join(process.work_directory, "boot.img")
        self.log(f"Boot files: {size // (1024 * 1024)} MiB, partition {kib // 1024} MiB")
        self.remote(f"""set -e
mkfs.vfat -F 32 -s 1 -n RPI-BOOT -C {quote(image)} {kib}
cd {quote(boot)}
export MTOOLS_SKIP_CHECK=1
mcopy -s -i {quote(image)} ./* ::/
""", "Failed to create boot partition")

class _StepWriteIso(DeployStep):
    def __init__(self, multistage_process):
        super().__init__(name="Create ISO", description="Writes new ISO with changes", multistage_process=multistage_process)

    def run(self):
        process = self.multistage_process
        work = process.work_directory
        temporary = process.prepared_iso + ".tmp"
        # Contents and El Torito boot of ISO are kept (xorriso writes new ISO only when its contents change).
        arguments = []
        if process.raspberry_pi is not None:
            # Boot configuration of Raspberry Pi in raspberry-pi folder. Partitions in MBR: FAT for Raspberry Pi first
            # (its firmware boots from it), EFI partition second (UEFI machines boot from USB drive with it).
            arguments += ["-boot_image any keep",
                          f"-map {quote(os.path.join(work, 'boot', 'config.txt'))} /raspberry-pi/config.txt",
                          f"-map {quote(os.path.join(work, 'boot', 'cmdline.txt'))} /raspberry-pi/cmdline.txt",
                          f"-append_partition 1 0x0c {quote(os.path.join(work, 'boot.img'))}",
                          f"-append_partition 2 0xef {quote(os.path.join(work, 'efi.img'))}"]
        else:
            arguments.append("-boot_image any replay") # Partitions of ISO (boot from USB drive) are kept too.
        if process.system is not None:
            arguments.append(f"-map {quote(process.live_directory + '/image.squashfs')} /image.squashfs")
        options = " \\\n    ".join(arguments)
        self.remote(f"""set -e
rm -f {quote(temporary)}
xorriso -indev {quote(process.iso_path)} -outdev {quote(temporary)} \\
    {options} \\
    -commit 2>&1 | grep -v '^xorriso : UPDATE' || true
test -s {quote(temporary)} || {{ echo "ISO was not created"; exit 1; }}
xorriso -indev {quote(temporary)} -report_system_area plain 2>/dev/null | grep -i "partition" || true
chmod a+r {quote(temporary)}
""", "Failed to create ISO")
        os.replace(temporary, process.prepared_iso)
        self.log(f"Created ISO ({os.path.getsize(process.prepared_iso) // (1024 * 1024)} MiB)")

class _StepWriteDisk(DeployStep):
    def __init__(self, multistage_process):
        super().__init__(name="Write to disk", description="Writes ISO to disk of this computer",
                         multistage_process=multistage_process)

    def run(self):
        process = self.multistage_process
        connection, disk = process.disk_connection, process.disk
        if connection is None or not connection.is_alive():
            raise RuntimeError("Root access ended, start again")
        if not os.path.isfile(process.prepared_iso):
            raise RuntimeError("ISO doesn't exist anymore")
        device = quote(disk.path)
        # Partitions mounted by desktop (inserted drive) are unmounted, disk is wiped of old signatures.
        self.remote(f"""set -e
for partition in $(lsblk -ln -o PATH,MOUNTPOINTS {device} | awk 'NF > 1 {{ print $1 }}'); do
    echo "Unmounting $partition"; umount "$partition"
done
wipefs -a {device} > /dev/null
""", f"Failed to prepare {disk.path}")
        size = os.path.getsize(process.prepared_iso)
        self.log(f"Writing {os.path.basename(process.prepared_iso)} ({size // (1024 * 1024)} MiB) to {disk.path}")
        if not connection.stream_file(process.prepared_iso, f"dd of={device} bs=4M iflag=fullblock oflag=direct status=none",
                                      self.log, lambda fraction: self._update_progress(fraction * 0.98), self.processes):
            raise RuntimeError(f"Failed to write {disk.path}")
        self.remote(f"sync; blockdev --rereadpt {device} 2>/dev/null || true; echo 'Written'", "Failed to finish writing")

class _StepSaveIso(DeployStep):
    def __init__(self, multistage_process):
        super().__init__(name="Save ISO", description="Saves ISO in selected location", multistage_process=multistage_process)

    def run(self):
        process = self.multistage_process
        source, destination = process.prepared_iso, process.output_path
        if not destination:
            raise RuntimeError("Location of ISO is not selected")
        partial = destination + ".part"
        size = os.path.getsize(source)
        if source != process.iso_path and os.stat(source).st_dev == os.stat(os.path.dirname(destination) or ".").st_dev:
            os.replace(source, destination) # Prepared ISO on the same disk is moved.
        else:
            # Copied with progress (ISO of build is kept, other disk).
            copied = 0
            with open(source, "rb") as input_file, open(partial, "wb") as output_file:
                while chunk := input_file.read(8 * 1024 * 1024):
                    if self._cancel_event.is_set():
                        raise RuntimeError("Canceled")
                    output_file.write(chunk)
                    copied += len(chunk)
                    self._update_progress(copied / size * 0.99)
            os.replace(partial, destination)
        self.log(f"Saved {destination} ({size // (1024 * 1024)} MiB)")
