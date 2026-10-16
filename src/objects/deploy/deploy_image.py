from __future__ import annotations
import base64, os, shlex, sys
from datetime import datetime
from .ssh_connection import quote
from .deploy_system import init_system_script

# ------------------------------------------------------------------------------
# Deploying to disk image file (.img): stage is installed like on disk of this computer, on loop device of image. Image
# is created bigger than needed (sparse file, unused space takes no space on disk), then its root filesystem is shrunk
# to its contents with some free space, and image is cut after it. Root partition is last, installed system grows it
# to fill the disk when it starts first time (catalystlab-grow-root). Image is compressed with xz, Raspberry Pi Imager
# and other tools write .img.xz directly.
# On Linux image is created through root helper (like disks of this computer). On macOS in virtual machine, where
# Catalyst Lab folder is shared at the same path, so image is created next to build.

# Free space left in root filesystem of image, for first start (logs, generated files) before it grows.
IMAGE_FREE_SPACE_RATIO = 0.2
IMAGE_MIN_FREE_SPACE = 256 * 1024 * 1024

def image_supported_reason(project_directory) -> str | None:
    """None when images can be created, otherwise reason why not."""
    if sys.platform.startswith("linux"):
        return None
    if image_machine(project_directory) is None:
        return "Not available: needs virtual machine, create one in Environments"
    return None

def image_machine(project_directory):
    """Virtual machine creating images on systems other than Linux: machine of project toolset, or first one."""
    from .lima import virtual_machines_supported
    from .repository import Repository
    if sys.platform.startswith("linux") or not virtual_machines_supported():
        return None
    toolset = project_directory.get_toolset() if project_directory else None
    if toolset is not None and getattr(toolset, "machine", None) is not None:
        return toolset.machine
    return next(iter(Repository.BuildMachine.value), None)

def image_connection(project_directory):
    """Connection running scripts as root where image is created."""
    if sys.platform.startswith("linux"):
        from .local_disk_connection import LocalDiskConnection
        return LocalDiskConnection()
    return MachineRootConnection(image_machine(project_directory))

def initial_image_size(archive_size: int) -> int:
    """Size of image before it's shrunk: enough for unpacked stage and installed packages (with Gentoo repository).
    Image is sparse, unused space takes no space on disk."""
    gib = 1024 ** 3
    return max(8 * gib, (archive_size * 8 + 8 * gib) // gib * gib)

def image_path(build) -> str:
    """New image file next to build archive."""
    name = (build.stage_name or "image").replace("/", "_")
    return os.path.join(build.path, f"{name}-{datetime.now().strftime('%Y%m%dT%H%M%S')}.img")

class MachineRootConnection:
    """Runs scripts as root in virtual machine. Machine is used (kept running) while connection is open."""
    is_local = True
    _USER = "Deploy image"

    def __init__(self, machine):
        self.machine = machine
        self.host = f"virtual machine {machine.name}" if machine else "virtual machine"
        self.connected = False

    def connect(self, password: str | None = None) -> str | None:
        if self.machine is None:
            return "No virtual machine to create image"
        self.machine.begin_use(self._USER)
        self.connected = True
        try:
            self.machine.ensure_running()
        except Exception as e:
            self.close()
            return f"Failed to start virtual machine {self.machine.name}: {e}"
        return None

    def close(self):
        if self.connected:
            self.connected = False
            self.machine.end_use(self._USER)

    def is_alive(self) -> bool:
        return self.connected and self.machine.is_running

    def _executor(self):
        from .rootless import MachineExecutor
        return MachineExecutor(self.machine)

    def run(self, script: str, output_handler=print, process_holder: list | None = None) -> bool:
        """Runs bash script as root in machine."""
        encoded = base64.b64encode(script.encode()).decode()
        command = (f'F=$(mktemp); echo {encoded} | base64 -d > "$F"; sudo bash "$F"; STATUS=$?; rm -f "$F"; exit $STATUS')
        return self._executor().run(command, output_handler, process_holder)

    def output(self, script: str) -> str:
        lines = []
        if not self.run(script, lines.append):
            raise RuntimeError(lines[-1] if lines else "Command failed")
        return "\n".join(lines)

    def stream_file(self, path: str, command: str, output_handler=print, progress_handler=None,
                    process_holder: list | None = None) -> bool:
        """Sends file (shared with machine at the same path) to stdin of command, with progress."""
        marker = "@@catalystlab-deploy-progress "
        program = f"""
import os, subprocess, sys
path, command = sys.argv[1], sys.argv[2]
size = os.path.getsize(path) or 1
process = subprocess.Popen(["bash", "-c", command], stdin=subprocess.PIPE)
sent, reported = 0, -1
try:
    with open(path, "rb") as file:
        while chunk := file.read(1024 * 1024):
            process.stdin.write(chunk)
            sent += len(chunk)
            if (percent := sent * 100 // size) != reported:
                reported = percent
                print("{marker}" + str(sent / size), flush=True)
    process.stdin.close()
except (BrokenPipeError, ValueError):
    pass
sys.exit(process.wait())
"""
        def handle_line(line: str):
            if line.startswith(marker):
                if progress_handler:
                    try:
                        progress_handler(float(line[len(marker):]))
                    except ValueError:
                        pass
            else:
                output_handler(line)
        return self.run(f"exec python3 -c {shlex.quote(program)} {shlex.quote(path)} {shlex.quote(command)}",
                        handle_line, process_holder)

# ------------------------------------------------------------------------------
# Scripts (bash, run as root).

def create_image_script(path: str, size: int) -> str:
    """Creates sparse image file and attaches it as loop device with partitions, prints device as LOOP=..."""
    image = quote(path)
    return f"""set -e
rm -f {image}
truncate -s {size} {image}
LOOP=$(losetup -f --show -P {image})
echo "Image {os.path.basename(path)} ({size // 1024 ** 3} GiB, sparse) attached as $LOOP"
echo "LOOP=$LOOP"
"""

def detach_image_script(path: str) -> str:
    """Detaches loop devices of image (after failure, or when it's done)."""
    return f"""for LOOP in $(losetup -j {quote(path)} | cut -d: -f1); do losetup -d "$LOOP" && echo "Detached $LOOP"; done; true"""

def shrink_image_script(path: str, loop: str, root_device: str, root_number: int, gpt: bool) -> str:
    """Shrinks ext4 root filesystem (last partition) to its contents with free space, partition after it, and cuts image
    after partition (with backup GPT header moved to new end)."""
    image, root, disk = quote(path), quote(root_device), quote(loop)
    ratio = int(IMAGE_FREE_SPACE_RATIO * 100)
    return f"""set -e
e2fsck -fy {root} > /dev/null || [ $? -le 1 ]
resize2fs -M {root} 2>&1 | grep -v "^resize2fs " || true
BLOCK_SIZE=$(dumpe2fs -h {root} 2>/dev/null | awk -F: '/^Block size/ {{gsub(/ /, "", $2); print $2}}')
BLOCKS=$(dumpe2fs -h {root} 2>/dev/null | awk -F: '/^Block count/ {{gsub(/ /, "", $2); print $2}}')
USED=$((BLOCK_SIZE * BLOCKS))
FREE=$((USED * {ratio} / 100)); [ $FREE -ge {IMAGE_MIN_FREE_SPACE} ] || FREE={IMAGE_MIN_FREE_SPACE}
# Filesystem size in whole MiB, partition gets the same size.
SIZE_MIB=$(( (USED + FREE + 1048575) / 1048576 ))
resize2fs {root} ${{SIZE_MIB}}M 2>&1 | grep -v "^resize2fs " || true
START=$(cat /sys/class/block/$(basename {root})/start)
SECTORS=$((SIZE_MIB * 2048))
echo ",$SECTORS" | sfdisk --no-reread --no-tell-kernel -N {root_number} {disk} > /dev/null 2>&1
END=$(( (START + SECTORS) * 512 ))
{'''# GPT is written again for shorter image (headers with its size, backup at its end). Partitions keep their UUIDs.
TABLE=$(sfdisk -d ''' + disk + ''' | grep -v "^last-lba:")
END=$((END + 1048576)) # Space for backup GPT.''' if gpt else ""}
losetup -d {disk}
truncate -s $END {image}
{'echo "$TABLE" | sfdisk --no-reread ' + image + ' > /dev/null 2>&1' if gpt else ""}
sfdisk -l {image} | tail -n +2
echo "Root filesystem: $((USED / 1048576)) MiB used, $SIZE_MIB MiB with free space"
echo "Image size: $((END / 1048576)) MiB"
"""

def compress_image_script(path: str) -> str:
    image = quote(path)
    return f"""set -e
rm -f {image}.xz
xz -T0 -6 {image}
echo "Compressed image: $(du -h {image}.xz | cut -f1) ({os.path.basename(path)}.xz)"
"""

# Installed in system of image: grows root partition (last on disk) and its filesystem to fill disk, at first start.
_GROW_ROOT_PROGRAM = r'''#!/bin/sh
# Installed by Catalyst Lab in disk image: grows root partition (last on disk) and its ext4 filesystem to fill the
# disk, when system starts first time. Removes itself when it's done.
DEVICE=$(mountpoint -d /) || exit 1
PART=/dev/$(basename "$(readlink -f "/sys/dev/block/$DEVICE")")
DISK=/dev/$(basename "$(readlink -f "/sys/dev/block/$DEVICE/..")")
NUMBER=$(cat "/sys/dev/block/$DEVICE/partition") || exit 1
echo "Growing $PART (partition $NUMBER of $DISK) to fill disk"
# Backup GPT header is moved to the end of disk (no-op error on MBR).
sfdisk --relocate gpt-bak-std "$DISK" 2> /dev/null || true
echo ", +" | sfdisk --no-reread --force -N "$NUMBER" "$DISK" || exit 1
partx -u "$DISK" || exit 1
resize2fs "$PART" || exit 1
rm -f /etc/local.d/catalystlab-grow-root.start /etc/systemd/system/multi-user.target.wants/catalystlab-grow-root.service \
    /etc/systemd/system/catalystlab-grow-root.service /usr/local/sbin/catalystlab-grow-root
echo "Root filesystem grows to fill disk: $(df -h / | tail -n 1)"
'''

_GROW_ROOT_SERVICE = """[Unit]
Description=Grow root partition to fill disk (Catalyst Lab image)
After=local-fs.target
ConditionPathExists=/usr/local/sbin/catalystlab-grow-root

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/catalystlab-grow-root

[Install]
WantedBy=multi-user.target
"""

def grow_root_script() -> str:
    """Installs catalystlab-grow-root in installed system ($ROOT), started once by OpenRC (local service) or systemd."""
    program = base64.b64encode(_GROW_ROOT_PROGRAM.encode()).decode()
    service = base64.b64encode(_GROW_ROOT_SERVICE.encode()).decode()
    return f"""set -e
mkdir -p "$ROOT/usr/local/sbin"
echo {program} | base64 -d > "$ROOT/usr/local/sbin/catalystlab-grow-root"
chmod 755 "$ROOT/usr/local/sbin/catalystlab-grow-root"
{init_system_script()}
if [ "$CL_INIT_SYSTEM" = openrc ]; then
    mkdir -p "$ROOT/etc/local.d" "$ROOT/etc/runlevels/default"
    printf '#!/bin/sh\\n/usr/local/sbin/catalystlab-grow-root\\n' > "$ROOT/etc/local.d/catalystlab-grow-root.start"
    chmod 755 "$ROOT/etc/local.d/catalystlab-grow-root.start"
    [ -e "$ROOT/etc/runlevels/default/local" ] || ln -s /etc/init.d/local "$ROOT/etc/runlevels/default/local"
    echo "Root filesystem grows to fill disk at first start (OpenRC local service)"
else
    mkdir -p "$ROOT/etc/systemd/system/multi-user.target.wants"
    echo {service} | base64 -d > "$ROOT/etc/systemd/system/catalystlab-grow-root.service"
    ln -sf /etc/systemd/system/catalystlab-grow-root.service "$ROOT/etc/systemd/system/multi-user.target.wants/catalystlab-grow-root.service"
    echo "Root filesystem grows to fill disk at first start (systemd service)"
fi
for TOOL in sfdisk partx resize2fs mountpoint; do
    ls "$ROOT"/usr/sbin/$TOOL "$ROOT"/usr/bin/$TOOL "$ROOT"/sbin/$TOOL "$ROOT"/bin/$TOOL > /dev/null 2>&1 \\
        || echo "Warning: $TOOL is not installed in system, root filesystem can't grow"
done
"""
