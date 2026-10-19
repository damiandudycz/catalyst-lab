from __future__ import annotations
import json, os, platform, shutil, signal, subprocess, sys, threading

# ------------------------------------------------------------------------------
# Lima (https://lima-vm.io) provides isolated Linux runners. On macOS it uses Virtualization.framework; on Linux it
# uses QEMU and 9p mounts. Folders of Catalyst Lab are shared with machines at the same paths.

def virtual_machines_supported() -> bool:
    """Virtual machines are available whenever Lima is installed and discoverable.

    The macOS application adds its bundled Lima directory to PATH. Linux installations
    discover Catalyst Lab's private Gentoo runtime first, then a system limactl executable.
    """
    return limactl_path() is not None

# GUI apps on macOS don't get PATH of shell, so Homebrew locations are checked too.
_LIMACTL_LOCATIONS = ["/opt/homebrew/bin/limactl", "/usr/local/bin/limactl"]

# Alpine image of machines. Linux kernel of Alpine supports everything needed to work without root (user namespaces,
# overlayfs, squashfs), and its packages provide bubblewrap and newuidmap.
LIMA_TEMPLATE = "template:_images/alpine-3.23"

# Temporary data that needs Linux ownership lives in a dedicated ext4 workspace image. Persistent extracted toolsets
# use ext4 images stored in Library/Cache; the shared folders themselves cannot preserve Linux owners or case.
MACHINE_DATA_DIRECTORY = "/var/lib/catalystlab"

# Machine disk contains only Alpine with tools, data of Catalyst Lab is in working space and shared folders.
SYSTEM_DISK_GIB = 8

# Lima checks that socket paths in instance directories are shorter than this (UNIX_PATH_MAX of macOS).
_MAX_SOCKET_PATH = 104
_INSTANCE_NAME_EXAMPLE = "catalystlab-00000000"

def runners_directory() -> str:
    """Managed VM runner homes, one folder per VM with its configuration, disks, and logs."""
    from .repository import Repository, resolve_path
    return resolve_path(Repository.Settings.value.runners_location)


def runner_temporary_directory(instance_name: str) -> str:
    """Temporary files for one VM runner's workspace and swap disk."""
    from .repository import library_path, resolve_path
    return resolve_path(library_path("Temporary", "Runners", instance_name))


def workspace_image_path(instance_name: str) -> str:
    """Temporary ext4 image shared with a machine while an operation is running."""
    return os.path.join(runner_temporary_directory(instance_name), "workspace.img")

def temporary_swap_disk_path(disk_name: str) -> str:
    """Lima swap disk data, beside its workspace in the per-machine Temporary folder."""
    instance_name = disk_name.removesuffix("-swap")
    return os.path.join(runner_temporary_directory(instance_name), "swap-disk")

def remove_empty_runner_temporary_directory(instance_name: str) -> None:
    """Remove a per-machine run folder after its workspace and swap disk are both gone."""
    directory = runner_temporary_directory(instance_name)
    for path in (directory, os.path.dirname(directory)):
        try:
            os.rmdir(path)
        except OSError:
            break

def move_lima_disk_to_temporary(disk_name: str, lima_home: str) -> str:
    """Move a newly created Lima disk into Temporary and leave its managed path as a symlink.

    Lima currently derives disk file paths from LIMA_HOME/_disks and has no per-disk
    location option. The symlink keeps Lima's disk management and attachment flow intact.
    """
    managed = os.path.join(lima_home, "_disks", disk_name)
    temporary = temporary_swap_disk_path(disk_name)
    if os.path.islink(managed) and os.path.realpath(managed) == temporary:
        return managed
    if not os.path.isdir(managed):
        raise FileNotFoundError(f"Lima disk directory not found: {managed}")
    os.makedirs(os.path.dirname(temporary), exist_ok=True)
    if os.path.lexists(temporary):
        shutil.rmtree(temporary) if os.path.isdir(temporary) and not os.path.islink(temporary) else os.unlink(temporary)
    shutil.move(managed, temporary)
    os.symlink(temporary, managed)
    return managed

def remove_temporary_lima_disk_data(disk_name: str, lima_home: str) -> None:
    """Remove a redirected disk's Temporary data and any leftover Lima symlink."""
    managed = os.path.join(lima_home, "_disks", disk_name)
    temporary = temporary_swap_disk_path(disk_name)
    if os.path.islink(managed) and os.path.realpath(managed) == os.path.realpath(temporary):
        os.unlink(managed)
    if os.path.lexists(temporary):
        shutil.rmtree(temporary) if os.path.isdir(temporary) and not os.path.islink(temporary) else os.unlink(temporary)
    remove_empty_runner_temporary_directory(disk_name.removesuffix("-swap"))

def default_lima_home() -> str:
    """~/.lima in the real home of user (not $HOME, which can be changed), used when the machine path is too long."""
    import pwd
    return os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".lima")

def lima_home() -> str:
    """Lima directory (LIMA_HOME) for new VM runners, or ~/.lima if runner paths would be too long."""
    directory = runners_directory()
    if lima_home_too_long(directory):
        print(f"Path of runner directory {directory} is too long for Lima, using ~/.lima")
        return default_lima_home()
    return directory

def lima_home_too_long(directory: str) -> bool:
    """Socket paths of machines in Lima directory would be too long."""
    return len(os.path.join(directory, _INSTANCE_NAME_EXAMPLE, "ssh.sock.1234567890123456")) >= _MAX_SOCKET_PATH

def lima_environment(home: str | None = None) -> dict:
    """Environment of limactl, with Lima directory of machine (or for new machines)."""
    environment = dict(os.environ)
    environment["LIMA_HOME"] = home or lima_home()
    if issue := _kvm_access_issue():
        # Lima 2.2.x checks only that /dev/kvm exists. If it exists but this process
        # cannot open it, Lima still chooses KVM and QEMU exits with EACCES. QEMU's
        # system override accepts extra arguments; set both conflicting options so
        # Lima's later defaults cannot re-enable KVM or select CPU type "host".
        qemu_arch, machine = _native_qemu_configuration()
        override = f"QEMU_SYSTEM_{qemu_arch.upper()}"
        if qemu_arch and override not in environment:
            environment[override] = f"qemu-system-{qemu_arch} -machine {machine},accel=tcg -cpu max"
    os.makedirs(environment["LIMA_HOME"], exist_ok=True)
    return environment

def _native_qemu_configuration() -> tuple[str | None, str | None]:
    """QEMU executable architecture and machine for the current native Linux host."""
    return {
        "aarch64": ("aarch64", "virt"),
        "arm64": ("aarch64", "virt"),
        "armv7l": ("arm", "virt"),
        "x86_64": ("x86_64", "q35"),
        "amd64": ("x86_64", "q35"),
        "riscv64": ("riscv64", "virt,acpi=off"),
        "ppc64le": ("ppc64", "pseries"),
        "s390x": ("s390x", "s390-ccw-virtio"),
    }.get(platform.machine().lower(), (None, None))

def _kvm_access_issue() -> str | None:
    """Returns why the current Linux process cannot use KVM, if applicable."""
    if not sys.platform.startswith("linux") or _native_qemu_configuration()[0] is None:
        return None
    try:
        descriptor = os.open("/dev/kvm", os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return "KVM is unavailable (/dev/kvm was not found). VM runners will use slower software emulation."
    except PermissionError:
        return "Your account cannot access /dev/kvm. Add it to the kvm group, then log out and back in to enable hardware acceleration."
    except OSError as error:
        return f"KVM is unavailable ({error}). VM runners may use slower software emulation."
    else:
        os.close(descriptor)
        return None

def limactl_path() -> str | None:
    if sys.platform.startswith("linux"):
        # Prefer the Gentoo USE=vm-runners runtime so builds use the version bundled for Catalyst Lab.
        private_path = "/usr/libexec/catalyst-lab/lima/bin/limactl"
        if os.access(private_path, os.X_OK):
            return private_path
    locations = list(_LIMACTL_LOCATIONS)
    return shutil.which("limactl") or next((path for path in locations if os.access(path, os.X_OK)), None)

def lima_unavailable_reason() -> str | None:
    """None when virtual machines can be created, otherwise reason why not."""
    if limactl_path() is None:
        if sys.platform == "darwin":
            return "Lima is not available. The macOS application bundle should include it."
        return "Lima (limactl) is not available on PATH. Install Lima for your system."
    return None

def kvm_access_warning() -> str | None:
    """Explains when Linux VM runners cannot use hardware acceleration."""
    return _kvm_access_issue()

def qemu_system_unavailable_warning() -> str | None:
    """Explains when the host lacks QEMU system emulation for its native architecture."""
    if not sys.platform.startswith("linux"):
        return None
    qemu_arch, _machine = _native_qemu_configuration()
    if not qemu_arch:
        return None
    if shutil.which(f"qemu-system-{qemu_arch}") is None:
        return (f"QEMU system emulation for this host ({qemu_arch}) is not installed. "
                f"Install qemu-system-{qemu_arch} to create VM runners.")
    return None

def qemu_9p_unavailable_warning() -> str | None:
    """Explains when the host QEMU lacks the 9p device required by Linux runner mounts."""
    if not sys.platform.startswith("linux"):
        return None
    qemu_arch, _machine = _native_qemu_configuration()
    if not qemu_arch:
        return None
    executable = shutil.which(f"qemu-system-{qemu_arch}")
    if not executable:
        return None # Lima reports the missing QEMU executable when the runner is started.
    try:
        result = subprocess.run([executable, "-device", "help"], capture_output=True, text=True,
                                 timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0 and "virtio-9p-pci" not in result.stdout + result.stderr:
        return "This QEMU build lacks virtio-9p-pci, which Lima needs to share folders. On Gentoo, enable the virtfs USE flag for app-emulation/qemu."
    return None

def list_instances(home: str | None = None) -> dict[str, dict]:
    """Lima instances by name, with their status (Running, Stopped...)."""
    path = limactl_path()
    if path is None:
        return {}
    try:
        output = subprocess.run([path, "list", "--json"], capture_output=True, text=True, timeout=30, env=lima_environment(home)).stdout
    except Exception as e:
        print(f"Failed to list Lima instances: {e}")
        return {}
    instances = {}
    for line in output.splitlines():
        try:
            instance = json.loads(line)
            instances[instance["name"]] = instance
        except (ValueError, KeyError):
            pass
    return instances

def run_limactl(arguments: list[str], output_handler, process_holder: list | None = None, home: str | None = None,
                timeout: float | None = None) -> bool:
    """Runs limactl with output passed to handler line by line. With timeout, limactl (and processes it started) is
    killed when it doesn't finish in time, and False is returned."""
    path = limactl_path()
    if path is None:
        raise RuntimeError(lima_unavailable_reason())
    output_handler(f"$ limactl {' '.join(arguments)}")
    process = subprocess.Popen([path, *arguments, "--tty=false"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, text=True, errors="replace", bufsize=1, start_new_session=True,
                               env=lima_environment(home))
    if process_holder is not None:
        process_holder.append(process)
    timed_out = threading.Event()
    def kill():
        timed_out.set()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    timer = threading.Timer(timeout, kill) if timeout else None
    if timer:
        timer.daemon = True
        timer.start()
    try:
        for line in process.stdout:
            output_handler(_clean_log_line(line.rstrip("\n")))
        result = process.wait()
    finally:
        if timer:
            timer.cancel()
    if timed_out.is_set():
        output_handler(f"limactl {arguments[0]} didn't finish in {int(timeout)} seconds")
        return False
    return result == 0

def instance_process_ids(instance_name: str, home: str | None = None) -> list[int]:
    """Host agent of instance and processes it started (driver of virtual machine), from its pid files."""
    directory = os.path.join(home or lima_home(), instance_name)
    pids = []
    for pid_file in ("ha.pid", "vz.pid", "qemu.pid"):
        try:
            with open(os.path.join(directory, pid_file), encoding="utf-8") as file:
                pid = int(file.read().strip())
        except (OSError, ValueError):
            continue
        children = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
        pids += [pid] + [int(child) for child in children if child.isdigit()]
    return list(dict.fromkeys(pids))

def kill_processes(pids: list[int], output_handler):
    """Kills processes that are still running."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            output_handler(f"Killed process {pid}")
        except OSError:
            pass # Already exited.

def kill_instance_processes(instance_name: str, output_handler, home: str | None = None, pids: list[int] | None = None):
    """Last resort when limactl can't stop instance: kills its host agent with processes it started (driver of virtual
    machine), also given ones (found before limactl removed pid files), and removes their pid and socket files, so
    instance is stopped."""
    kill_processes(list(dict.fromkeys((pids or []) + instance_process_ids(instance_name, home))), output_handler)
    directory = os.path.join(home or lima_home(), instance_name)
    for name in ("ha.pid", "ha.sock", "vz.pid", "qemu.pid", "default_ep.sock", "default_fd.sock"):
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass

def instance_output(instance_name: str, command: list[str], home: str | None = None, timeout: float = 15) -> str | None:
    """Output of short command run in running instance (as user), None when it fails."""
    path = limactl_path()
    if path is None:
        return None
    try:
        result = subprocess.run([path, "shell", "--workdir", "/", instance_name, *command], capture_output=True, text=True,
                                errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, env=lima_environment(home))
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None

def run_as_root_in_instance(instance_name: str, script: str, output_handler, home: str | None = None) -> bool:
    """Runs shell script as root in running instance, output goes to handler line by line."""
    path = limactl_path()
    if path is None:
        raise RuntimeError(lima_unavailable_reason())
    process = subprocess.Popen([path, "shell", "--workdir", "/", instance_name, "sudo", "sh", "-c", script],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                               errors="replace", bufsize=1, start_new_session=True, env=lima_environment(home))
    for line in process.stdout:
        line = line.rstrip("\n")
        if not (line.startswith("time=") and "level=" in line): # Lima warnings.
            output_handler(line)
    return process.wait() == 0

def swap_activation_script(size_bytes: int) -> str:
    """Turns on swap on additional disk of machine (as root). Disk is found by its size, only empty disk without
    partitions, filesystem or mounts is used, so system disk is never formatted."""
    return f"""set -e
SIZE={size_bytes}
DEVICE=""
for NAME in $(lsblk -dnro NAME,TYPE | awk '$2 == "disk" {{ print $1 }}'); do
    [ "$(lsblk -dnbro SIZE "/dev/$NAME")" = "$SIZE" ] || continue
    [ "$(lsblk -dnro RO "/dev/$NAME")" = "0" ] || continue
    [ "$(lsblk -nro NAME "/dev/$NAME" | wc -l)" = "1" ] || continue # Has partitions.
    findmnt -rn -S "/dev/$NAME" > /dev/null && continue
    if command -v blkid > /dev/null; then
        TYPE=$(blkid -p -o value -s TYPE "/dev/$NAME" 2>/dev/null || true)
        PTTYPE=$(blkid -p -o value -s PTTYPE "/dev/$NAME" 2>/dev/null || true)
        [ -z "$PTTYPE" ] || continue
        [ -z "$TYPE" ] || [ "$TYPE" = swap ] || continue
    fi
    DEVICE="/dev/$NAME"
    break
done
[ -n "$DEVICE" ] || {{ echo "Swap disk was not found in machine"; exit 1; }}
grep -q "^$DEVICE " /proc/swaps && {{ echo "Swap is on $DEVICE"; exit 0; }}
mkswap -L catalystlab-swap "$DEVICE" > /dev/null
swapon "$DEVICE"
echo "Swap: $((SIZE / 1073741824)) GiB on $DEVICE"
"""

def _clean_log_line(line: str) -> str:
    """Lima logs as time="..." level=info msg="...", only message is shown."""
    if line.startswith("time=") and ' msg="' in line:
        message = line.split(' msg="', 1)[1]
        return message[:-1].replace('\\"', '"') if message.endswith('"') else message
    return line

def machine_configuration(cpus: int, memory_gib: int, shared_paths: list[str]) -> str:
    """Lima configuration (YAML) of build machine."""
    mounts = "\n".join(f'- location: "{path}"\n  writable: true' for path in shared_paths)
    indented_setup = "\n".join("    " + line for line in machine_setup_script().splitlines())
    vm_type = "vz" if sys.platform == "darwin" else "qemu"
    mount_type = "" if sys.platform == "darwin" else "mountType: 9p\n"
    return f"""minimumLimaVersion: 2.0.0
base:
- {LIMA_TEMPLATE}
vmType: {vm_type}
{mount_type}cpus: {cpus}
memory: {memory_gib}GiB
disk: {SYSTEM_DISK_GIB}GiB
containerd:
  system: false
  user: false
mounts:
{mounts}
provision:
- mode: system
  script: |
{indented_setup}
"""

# Provisioning marker: setup is run once when a machine is created.
_MACHINE_SETUP_MARKER = "/etc/catalystlab-setup-complete"

# QEMU user emulators, to build stages for other architectures (like on Linux hosts, binfmt_misc with F flag makes
# them work in chroots and namespaces).
_QEMU_ARCHITECTURES = "x86_64 i386 arm armeb aarch64 riscv64 ppc ppc64 ppc64le s390x mips mipsel mips64 mips64el sparc sparc64 alpha m68k loongarch64 hppa sh4"

# On Apple Silicon, Lima can register Apple's Rosetta runtime as the x86_64 Linux
# binfmt handler. Keep QEMU available for other target architectures.
_ROSETTA_FOR_AMD64 = sys.platform == "darwin" and platform.machine().lower() in ("arm64", "aarch64")

# Rules of qemu-binfmt (Alpine) for some architectures (eg. ppc64) match only ELF files with OS ABI 0 (SYSV), programs
# marked with GNU/Linux ABI (3), like gcc and ldconfig of Gentoo ppc64 stages, fail then with Exec format error. Rules
# are registered again on every boot (after qemu-binfmt, by local service), ignoring OS ABI and ABI version bytes of
# ELF header, like rules of QEMU for most architectures.
_BINFMT_FIX_SCRIPT = r"""#!/bin/sh
cd /proc/sys/fs/binfmt_misc || exit 0
# Lima registers Rosetta before provisioning when enabled. QEMU's amd64 handler
# must be removed so amd64 ELF files are dispatched to Rosetta instead.
if [ -f rosetta ] && grep -q '^enabled' rosetta && [ -f qemu-x86_64 ]; then
    echo -1 > qemu-x86_64
    echo "Using Lima's Rosetta handler for x86_64 Linux binaries"
fi
for entry in qemu-*; do
    [ -f "$entry" ] || continue
    offset=$(sed -n 's/^offset //p' "$entry")
    magic=$(sed -n 's/^magic //p' "$entry")
    mask=$(sed -n 's/^mask //p' "$entry")
    interpreter=$(sed -n 's/^interpreter //p' "$entry")
    flags=$(sed -n 's/^flags: //p' "$entry")
    [ "$offset" = 0 ] && [ -n "$magic" ] && [ -n "$mask" ] || continue
    # Bytes 7 and 8 of ELF header are hex digits 15-18.
    fixed_mask="$(echo "$mask" | cut -c1-14)0000$(echo "$mask" | cut -c19-)"
    [ "$fixed_mask" = "$mask" ] && continue
    echo -1 > "$entry"
    printf ':%s:M:0:%s:%s:%s:%s\n' "$entry" "$(echo "$magic" | sed 's/../\\x&/g')" \
        "$(echo "$fixed_mask" | sed 's/../\\x&/g')" "$interpreter" "$flags" > register
done
"""

def machine_setup_script() -> str:
    """Prepares machine (as root, idempotent): tools for working without root, subordinate ids of user, working space
    mount point and emulators of other architectures."""
    return f"""#!/bin/sh
set -eux
# Provisioning runs on every boot; install the machine tools only once.
test -f {_MACHINE_SETUP_MARKER} && exit 0
# Mirror sometimes stops sending data in the middle of download (for a minute or more): downloads time out quickly
# and are retried. Downloaded packages are kept in cache, so retries download only missing ones.
mkdir -p /var/cache/catalystlab-apk
apk_options="--cache-dir /var/cache/catalystlab-apk"
apk --timeout 20 --version >/dev/null 2>&1 && apk_options="$apk_options --timeout 20"
retry() {{
    for attempt in 1 2 3 4 5; do
        "$@" && return 0
        echo "Retrying: $*"
        sleep 2
    done
    return 1
}}
retry apk update $apk_options
host=$(uname -m)
emulators=""
for arch in {_QEMU_ARCHITECTURES}; do
    if [ "$arch" = "$host" ]; then
        continue
    elif apk search $apk_options "qemu-$arch" | grep -q "^qemu-$arch-[0-9]"; then
        emulators="$emulators qemu-$arch"
    else
        echo "qemu-$arch is not available"
    fi
done
# One transaction: package index is read once and packages are downloaded together.
retry apk add $apk_options --cache-predownload bash coreutils findutils grep sed tar xz zstd bubblewrap util-linux util-linux-misc \\
    shadow-subids squashfs-tools python3 curl e2fsprogs dosfstools mtools xorriso xz qemu-openrc $emulators
rm -rf /var/cache/catalystlab-apk
# Remove the old script interpreter: binfmt_misc needs a directly executable interpreter here.
rm -f /usr/local/bin/catalystlab-qemu-x86_64
rc-update add qemu-binfmt default
rc-service qemu-binfmt restart || rc-service qemu-binfmt start
mkdir -p /etc/local.d
echo {_shell_base64(_BINFMT_FIX_SCRIPT)} | base64 -d > /etc/local.d/catalystlab-binfmt.start
chmod +x /etc/local.d/catalystlab-binfmt.start
rc-update add local default
/etc/local.d/catalystlab-binfmt.start
# User of machine (same uid as on host) gets subordinate ids for files of toolsets.
user=$(getent passwd | awk -F: '$3 >= 500 && $3 < 60000 {{ print $1; exit }}')
grep -q "^$user:" /etc/subuid || echo "$user:100000:65536" >> /etc/subuid
grep -q "^$user:" /etc/subgid || echo "$user:100000:65536" >> /etc/subgid
mountpoint -q {MACHINE_DATA_DIRECTORY} || install -d -o "$user" {MACHINE_DATA_DIRECTORY}
touch {_MACHINE_SETUP_MARKER}
"""

def _shell_base64(text: str) -> str:
    import base64
    return base64.b64encode(text.encode()).decode()

