from __future__ import annotations
import base64, functools, json, os, re, shlex, shutil, signal, subprocess, tempfile, threading, uuid
from .repository import Repository, resolve_path

# ------------------------------------------------------------------------------
# Working without root privileges.
# Commands run as root of unprivileged user namespace, where ids 1..65536 are mapped to subordinate ids of user
# (/etc/subuid, /etc/subgid), so files can have their real owners (portage, shadow...). Things that need real root
# are replaced: on local rootless execution SquashFS files are extracted (loop mounts are not allowed), while a build
# machine can mount them read-only with its real root; host /dev is bound instead of creating device nodes. Files of
# namespace root are owned by user, other owners are mapped ids (100000+), so such files are created, packed and
# removed inside namespace.
# Commands run on this computer (LocalExecutor) or in virtual machine linked to toolset (MachineExecutor), where
# folders of Catalyst Lab are shared at the same paths. Data owned by mapped ids is kept in cache and temporary
# directories of executor: on this computer they are Cache and Temporary folders from settings, on machine they are on
# its working space.

MAPPED_IDS_COUNT = 65536
MIN_BWRAP_VERSION = (0, 11, 0) # Toolset bindings use --overlay-src and --overlay.

# Runs the script at $1 in a user namespace with mapped ids and isolated mount, pid, uts and ipc namespaces. Waits for
# uid/gid mapping before starting it because newuidmap is called from outside the namespace.
_NAMESPACE_LAUNCHER = r'''
set -u
INNER=$(mktemp); FIFO=$(mktemp -u)
trap 'rm -f "$INNER" "$FIFO"' EXIT
cat "$1" > "$INNER"
mkfifo "$FIFO"
read -r SUBUID_START SUBUID_COUNT < <(awk -F: -v u="$(id -un)" -v i="$(id -u)" '($1==u||$1==i){print $2, ($3>65536?65536:$3); exit}' /etc/subuid)
read -r SUBGID_START SUBGID_COUNT < <(awk -F: -v u="$(id -un)" -v i="$(id -u)" '($1==u||$1==i){print $2, ($3>65536?65536:$3); exit}' /etc/subgid)
[ -n "${SUBUID_START:-}" ] && [ -n "${SUBGID_START:-}" ] || { echo "User has no entries in /etc/subuid and /etc/subgid"; exit 1; }
unshare --user bash -c 'read _ < "$1"; exec unshare --mount --pid --uts --ipc --fork --kill-child --propagation private bash "$2"' _ "$FIFO" "$INNER" < /dev/null &
NS=$!
OWN=$(readlink /proc/self/ns/user)
for _ in $(seq 500); do [ "$(readlink /proc/$NS/ns/user 2>/dev/null)" != "$OWN" ] && break; sleep 0.02; done
if ! newuidmap $NS 0 "$(id -u)" 1 1 "$SUBUID_START" "$SUBUID_COUNT" || ! newgidmap $NS 0 "$(id -g)" 1 1 "$SUBGID_START" "$SUBGID_COUNT"; then
    echo "Failed to map user ids"; kill $NS; exit 1
fi
echo go > "$FIFO"
wait $NS
'''

def _filesystem_operations(request: dict) -> dict:
    """Filesystem operations in toolset folders, which can be on machine (source of this function runs there).
    Request can contain: info (paths), list (directories), mkdir (paths), symlink ([target, path]), write ([path,
    content]), remove (files). Returns info and list results."""
    import os, stat
    result = {}
    if "info" in request:
        info = {}
        for path in request["info"]:
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                info[path] = {"type": None}
                continue
            mode = st.st_mode
            kind = ("link" if stat.S_ISLNK(mode) else "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode)
                    else "chr" if stat.S_ISCHR(mode) else "blk" if stat.S_ISBLK(mode) else "other")
            info[path] = {"type": kind, "target": os.readlink(path) if kind == "link" else None}
        result["info"] = info
    if "list" in request:
        result["list"] = {
            path: sorted([entry.name, "dir" if entry.is_dir() else "file" if entry.is_file() else "other"] for entry in os.scandir(path))
            if os.path.isdir(path) else None
            for path in request["list"]
        }
    for path in request.get("mkdir", []):
        os.makedirs(path, exist_ok=True)
    for target, path in request.get("symlink", []):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)
    for path, content in request.get("write", []):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            file.write(content)
    for path in request.get("remove", []):
        if os.path.isfile(path) or os.path.islink(path):
            os.remove(path)
    return result

class ExecutorProcess:
    """Running script. Output is in stdout (text lines), like subprocess.Popen."""
    def __init__(self, process: subprocess.Popen, on_terminate=None, on_exit=None):
        self.process = process
        self.stdout = process.stdout
        self.canceled = False
        self._on_terminate = on_terminate
        self._on_exit = on_exit # Called once when process exits.
    def _exited(self):
        on_exit, self._on_exit = self._on_exit, None
        if on_exit:
            on_exit()
    def wait(self, timeout=None) -> int:
        code = self.process.wait(timeout=timeout)
        self._exited()
        return code
    def poll(self):
        code = self.process.poll()
        if code is not None:
            self._exited()
        return code
    def terminate(self):
        """Stops script with all its processes (killing pid namespace init ends whole namespace)."""
        self.canceled = True
        if self._on_terminate:
            try:
                self._on_terminate()
            except Exception as e:
                print(f"Failed to stop remote process: {e}")
            try:
                self.process.wait(timeout=5) # Ends with remote process.
            except subprocess.TimeoutExpired:
                pass
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError): # Already exited (macOS reports EPERM for exited group).
                pass
        if self.process.poll() is not None:
            self._exited()
    def kill(self):
        self.terminate()

class Executor:
    """Runs rootless commands. Scripts run with bash, output goes to output handler line by line."""
    is_machine = False

    def cache_directory(self) -> str:
        """Reusable data, safe to delete (distfiles, extracted toolsets and snapshots)."""
        raise NotImplementedError

    def temporary_directory(self) -> str:
        """Data of running operations (sessions, work directories, downloads)."""
        raise NotImplementedError

    def owns_path(self, path: str) -> bool:
        """Path is in cache or temporary directory of executor, its files can be owned by mapped ids."""
        return any(path == directory or path.startswith(directory + os.sep)
                   for directory in (self.cache_directory(), self.temporary_directory()))

    def unsupported_reason(self) -> str | None:
        """None when commands can run without root."""
        raise NotImplementedError

    def toolset_unsupported_reason(self, output_handler=None) -> str | None:
        """None when toolset environments (bwrap) can run without root."""
        raise NotImplementedError

    def start(self, script: str, namespace: bool = False) -> ExecutorProcess:
        raise NotImplementedError

    def run(self, script: str, output_handler, process_holder: list | None = None, namespace: bool = False) -> bool:
        process = self.start(script, namespace=namespace)
        if process_holder is not None:
            process_holder.append(process)
        for line in process.stdout:
            output_handler(line.rstrip("\n"))
        return process.wait() == 0 and not process.canceled

    def run_in_namespace(self, script: str, output_handler, process_holder: list | None = None) -> bool:
        return self.run(script, output_handler, process_holder, namespace=True)

    def output(self, script: str) -> str | None:
        """Output of script, None when it fails."""
        lines = []
        return "\n".join(lines) if self.run(script, lines.append) else None

    def filesystem(self, request: dict) -> dict:
        """Filesystem operations, see _filesystem_operations."""
        import inspect
        program = (inspect.getsource(_filesystem_operations)
                   + "\nimport json, sys\nprint(json.dumps(_filesystem_operations(json.loads(sys.argv[1]))))\n")
        lines = []
        if not self.run(f"exec python3 -c {shlex.quote(program)} {shlex.quote(json.dumps(request))}", lines.append):
            raise RuntimeError(f"Filesystem operation failed: {lines[-1] if lines else 'no output'}")
        return json.loads(lines[-1])

class LocalExecutor(Executor):
    """Runs commands on this computer."""

    def cache_directory(self) -> str:
        return resolve_path(Repository.Settings.value.cache_location)

    def temporary_directory(self) -> str:
        return resolve_path(Repository.Settings.value.temporary_location)

    def unsupported_reason(self) -> str | None:
        return rootless_unsupported_reason()

    def toolset_unsupported_reason(self, output_handler=None) -> str | None:
        return rootless_toolset_unsupported_reason()

    def start(self, script: str, namespace: bool = False) -> ExecutorProcess:
        if namespace:
            arguments = ["bash", "-c", _NAMESPACE_LAUNCHER, "launcher", base64.b64encode(script.encode()).decode()]
        else:
            arguments = ["bash", "-c", script]
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True, text=True, errors="replace", bufsize=1)
        return ExecutorProcess(process)

    def filesystem(self, request: dict) -> dict:
        return _filesystem_operations(request)

class MachineExecutor(Executor):
    """Runs commands in virtual machine (Lima instance), started when needed."""
    is_machine = True

    def __init__(self, machine):
        self.machine = machine

    # Reusable files are stored in the configured Library cache, which Lima shares at the same host path. Temporary
    # operation data remains in the machine's disposable working space.
    def cache_directory(self) -> str:
        return resolve_path(Repository.Settings.value.cache_location)

    def temporary_directory(self) -> str:
        from .lima import MACHINE_DATA_DIRECTORY
        return os.path.join(MACHINE_DATA_DIRECTORY, "Temporary")

    def unsupported_reason(self) -> str | None:
        return self.toolset_unsupported_reason()

    def toolset_unsupported_reason(self, output_handler=None) -> str | None:
        from .lima import lima_unavailable_reason
        if reason := lima_unavailable_reason():
            return reason
        if self.machine.status == self.machine.STATUS_MISSING:
            return f"Virtual machine {self.machine.name} doesn't exist"
        lines = []
        script = r'''
for tool in unshare newuidmap newgidmap bwrap unsquashfs mksquashfs python3 mount mountpoint sudo; do
    command -v $tool > /dev/null || { echo "$tool is not installed in machine"; exit 1; }
done
grep -q "^$(id -un):" /etc/subuid || { echo "Machine user has no entries in /etc/subuid"; exit 1; }
grep -q "^$(id -un):" /etc/subgid || { echo "Machine user has no entries in /etc/subgid"; exit 1; }
unshare --user --map-root-user true || { echo "User namespaces are not available in machine"; exit 1; }
echo "Machine is ready: $(. /etc/os-release; echo $PRETTY_NAME), kernel $(uname -r), $(bwrap --version)"
'''
        success = self.run(script, lines.append)
        for line in lines:
            (output_handler or print)(line)
        return None if success else (lines[-1] if lines else "Machine check failed")

    def start(self, script: str, namespace: bool = False) -> ExecutorProcess:
        from .lima import limactl_path, lima_environment
        # Machine is used while command runs, it's started when needed and stopped automatically when no longer used.
        self.machine.begin_use()
        try:
            self.machine.ensure_running()
        except Exception:
            self.machine.end_use()
            raise
        token = uuid.uuid4().hex
        pid_file = f"/tmp/catalystlab-{token}.pid"
        # Long scripts exceed the command payload limit of Lima's SSH multiplexing channel (mm_send_fd:
        # "Message too long"). Put the script in the shared Library Temporary folder and pass only its short path.
        try:
            script_directory = os.path.join(
                resolve_path(Repository.Settings.value.temporary_location), "MachineCommands")
            os.makedirs(script_directory, exist_ok=True)
            descriptor, script_path = tempfile.mkstemp(prefix=f"{token}-", suffix=".sh", dir=script_directory)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(script)
        except Exception:
            if "script_path" in locals():
                try:
                    os.unlink(script_path)
                except FileNotFoundError:
                    pass
            self.machine.end_use()
            raise
        q = shlex.quote
        if namespace:
            command = f"bash -c {q(_NAMESPACE_LAUNCHER)} launcher {q(script_path)}"
        else:
            command = f"bash {q(script_path)}; status=$?; rm -f {q(script_path)}; exit $status"
        # Own session in machine, so canceling can stop its whole process group.
        bootstrap = f"echo $$ > {pid_file}; exec 2>&1 < /dev/null; {command}"
        try:
            process = subprocess.Popen(
                [limactl_path(), "shell", "--workdir", "/", self.machine.instance_name, "setsid", "-w", "bash", "-c", bootstrap],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                text=True, errors="replace", bufsize=1, env=lima_environment(self.machine.lima_home)
            )
        except Exception:
            os.unlink(script_path)
            self.machine.end_use()
            raise
        def terminate_remote():
            subprocess.run([limactl_path(), "shell", self.machine.instance_name, "sh", "-c",
                            f"[ -f {pid_file} ] && kill -TERM -- -$(cat {pid_file}); rm -f {pid_file}"],
                           capture_output=True, timeout=30, env=lima_environment(self.machine.lima_home))
        def cleanup_script():
            try:
                os.unlink(script_path)
            except FileNotFoundError:
                pass
            self.machine.end_use()
        return ExecutorProcess(process, on_terminate=terminate_remote, on_exit=cleanup_script)

    def run(self, script: str, output_handler, process_holder: list | None = None, namespace: bool = False) -> bool:
        # Lima writes its warnings to output, they are not part of script output.
        return super().run(script, lambda line: None if line.startswith("time=") and "level=" in line else output_handler(line),
                           process_holder, namespace)

LOCAL_EXECUTOR = LocalExecutor()

def executor_for_machine(machine) -> Executor:
    return MachineExecutor(machine) if machine else LOCAL_EXECUTOR

# ------------------------------------------------------------------------------
# Local support.

def is_rootless_path(path: str | None) -> bool:
    """Path is in cache or temporary directory of this computer, its files are owned by user or mapped ids, not by
    real root."""
    return bool(path) and LOCAL_EXECUTOR.owns_path(os.path.realpath(path))

@functools.cache
def rootless_toolset_unsupported_reason() -> str | None:
    """None when toolset environments (bwrap) can run without root on this computer, otherwise reason why not."""
    if reason := rootless_unsupported_reason():
        return reason
    path = shutil.which("bwrap")
    if path is None:
        return "bwrap is not installed"
    try:
        output = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout
        version = tuple(int(number) for number in re.findall(r"\d+", output)[:3])
    except Exception as e:
        return f"Failed to check bwrap version: {e}"
    if version < MIN_BWRAP_VERSION:
        return f"bwrap {'.'.join(map(str, MIN_BWRAP_VERSION))} or newer is required"
    return None

@functools.cache
def rootless_unsupported_reason() -> str | None:
    """None when commands can run without root on this computer, otherwise reason why not. Checked once while app runs."""
    if os.environ.get("CATALYSTLAB_ROOTLESS", "1") == "0":
        return "Disabled with CATALYSTLAB_ROOTLESS=0"
    for tool in ("unshare", "newuidmap", "newgidmap", "unsquashfs"):
        if shutil.which(tool) is None:
            return f"{tool} is not installed"
    if _subordinate_range("/etc/subuid") is None or _subordinate_range("/etc/subgid") is None:
        return "User has no entries in /etc/subuid and /etc/subgid"
    try:
        result = subprocess.run(["unshare", "--user", "--map-root-user", "true"], capture_output=True, timeout=10)
        if result.returncode != 0:
            return f"Unprivileged user namespaces are not available: {result.stderr.decode(errors='replace').strip()}"
    except Exception as e:
        return f"Unprivileged user namespaces are not available: {e}"
    return None

def _subordinate_range(path: str) -> tuple[int, int] | None:
    import pwd
    names = {str(os.getuid()), pwd.getpwuid(os.getuid()).pw_name}
    try:
        with open(path, encoding="utf-8") as file:
            for line in file:
                parts = line.strip().split(":")
                if len(parts) == 3 and parts[0] in names and int(parts[2]) > 0:
                    return int(parts[1]), min(int(parts[2]), MAPPED_IDS_COUNT)
    except (OSError, ValueError):
        pass
    return None

def run_in_namespace(script: str, output_handler, process_holder: list | None = None, executor: Executor | None = None) -> bool:
    """Runs script in namespace, process is appended to process_holder (to allow canceling)."""
    return (executor or LOCAL_EXECUTOR).run_in_namespace(script, output_handler, process_holder)

# ------------------------------------------------------------------------------
# Extracted toolsets and snapshots.
# Files keep their owners (mapped ids), so they are extracted and removed inside namespace.

def extracted_squashfs(squashfs_path: str, kind: str, output_handler, process_holder: list | None = None,
                       check_path: str | None = None, executor: Executor | None = None) -> str:
    """Folder with extracted squashfs file, extracted again when file changed. Kind is subfolder of extracted files
    (toolsets, snapshots). Device nodes can't be created in namespace and are skipped."""
    executor = executor or LOCAL_EXECUTOR
    q = shlex.quote
    name = os.path.basename(squashfs_path)
    parent = extracted_directory(kind, executor)
    path = os.path.join(parent, name + ".d")
    # Stage beside the cache destination so publishing the extracted tree is a same-filesystem
    # rename. MachineExecutor's temporary directory is on the VM's local disk while its cache
    # is a host-mounted library volume; moving between them makes mv recursively copy/merge and
    # can fail when a previous interrupted extraction left non-empty directories behind.
    temporary_path = os.path.join(parent, f".{name}.{uuid.uuid4().hex}.tmp")
    script = f"""
set -euo pipefail
STAMP=$(stat -c '%s:%Y' {q(squashfs_path)}) || exit 1
if [ -d {q(path)} ] && [ "$(cat {q(path + '.stamp')} 2>/dev/null)" = "$STAMP" ]; then
    echo "Using extracted {name}"
    exit 0
fi
echo "Extracting {name}, this is done once for every version of the file..."
mkdir -p {q(parent)}
rm -rf -- {q(path)} {q(path + '.stamp')}
TEMP_PATH={q(temporary_path)}
CHECK_PATH={q(check_path or '')}
rm -rf -- "$TEMP_PATH"
trap 'rm -rf -- "$TEMP_PATH"' EXIT
unsquashfs -n -d "$TEMP_PATH" {q(squashfs_path)} 2>&1 | {{ grep -v -e "create_inode: failed to create character device" -e "^\\[" -e "^$" || [ "$?" -eq 1 ]; }} | tail -5
if [ ! -e "$TEMP_PATH/$CHECK_PATH" ]; then
    echo "Extraction failed"
    exit 1
fi
mv -T -- "$TEMP_PATH" {q(path)}
trap - EXIT
echo "$STAMP" > {q(path + '.stamp')}
"""
    if not executor.run_in_namespace(script, output_handler, process_holder):
        raise RuntimeError(f"Failed to extract {name}")
    return path

def mount_squashfs_read_only(squashfs_path: str, output_handler, process_holder: list | None = None,
                             executor: Executor | None = None, kind: str = "toolsets") -> str:
    """Mounts a SquashFS image read-only in a Linux build machine, without extracting its files."""
    executor = executor or LOCAL_EXECUTOR
    if not executor.is_machine:
        raise RuntimeError("Direct SquashFS mounts are only supported in build machines")
    q = shlex.quote
    mount_point = create_work_directory(f"{kind}/mount_", executor)
    output_handler(f"Mounting {os.path.basename(squashfs_path)} read-only")
    script = f"""
set -euo pipefail
sudo mount -t squashfs -o loop,ro {q(squashfs_path)} {q(mount_point)}
mountpoint -q {q(mount_point)}
"""
    if not executor.run(script, output_handler, process_holder):
        cleanup_script = f"""
if mountpoint -q {q(mount_point)}; then sudo umount -- {q(mount_point)}; fi
rmdir -- {q(mount_point)} 2>/dev/null || true
"""
        executor.run(cleanup_script, output_handler)
        raise RuntimeError(f"Failed to mount {os.path.basename(squashfs_path)} read-only")
    return mount_point

def prepare_squashfs(squashfs_path: str, kind: str, check_path: str, output_handler,
                     process_holder: list | None = None, executor: Executor | None = None) -> tuple[str, bool]:
    """Prepare a read-only SquashFS lower layer: mount in a build machine, or use the extracted cache locally."""
    executor = executor or LOCAL_EXECUTOR
    if executor.is_machine:
        return mount_squashfs_read_only(squashfs_path, output_handler, process_holder, executor, kind=kind), True
    return extracted_squashfs(squashfs_path, kind, output_handler, process_holder,
                              check_path=check_path, executor=executor), False

def prepare_toolset_squashfs(squashfs_path: str, output_handler, process_holder: list | None = None,
                             executor: Executor | None = None) -> tuple[str, bool]:
    """Prepare a toolset layer; stage builds add a writable OverlayFS upper layer to it."""
    return prepare_squashfs(squashfs_path, "toolsets", "bin/bash", output_handler, process_holder, executor)

def prepare_snapshot_squashfs(squashfs_path: str, output_handler, process_holder: list | None = None,
                              executor: Executor | None = None) -> tuple[str, bool]:
    """Prepare a Gentoo repository layer."""
    return prepare_squashfs(squashfs_path, "snapshots", "profiles", output_handler, process_holder, executor)

def unmount_squashfs(mount_point: str, output_handler, process_holder: list | None = None,
                     executor: Executor | None = None) -> None:
    """Unmounts a read-only SquashFS mounted in a Linux build machine and removes its mountpoint."""
    executor = executor or LOCAL_EXECUTOR
    if not executor.is_machine:
        raise RuntimeError("Direct SquashFS mounts are only supported in build machines")
    q = shlex.quote
    script = f"""
set -euo pipefail
if mountpoint -q {q(mount_point)}; then
    sudo umount -- {q(mount_point)}
fi
rmdir -- {q(mount_point)}
"""
    if not executor.run(script, output_handler, process_holder):
        raise RuntimeError(f"Failed to unmount SquashFS at {mount_point}")

def remove_extracted_squashfs(squashfs_path: str, kind: str, executor: Executor | None = None):
    """Removes extracted copy of squashfs file (eg. when toolset or snapshot is deleted), in background."""
    executor = executor or LOCAL_EXECUTOR
    if executor.is_machine:
        return # Machine extracted trees are inside the disposable ext4 workspace.
    if rootless_unsupported_reason() is not None:
        return
    path = os.path.join(extracted_directory(kind, executor), os.path.basename(squashfs_path) + ".d")
    threading.Thread(target=remove_in_namespace, args=([path, path + ".stamp"], print, None, executor), daemon=True).start()

def remove_in_namespace(paths: list[str], output_handler, process_holder: list | None = None, executor: Executor | None = None) -> bool:
    """Removes files that can be owned by mapped ids."""
    if not paths:
        return True
    return (executor or LOCAL_EXECUTOR).run_in_namespace("rm -rf -- " + " ".join(shlex.quote(path) for path in paths), output_handler, process_holder)

# ------------------------------------------------------------------------------
# Folders of executor:
# Cache/Distfiles             sources downloaded by portage
# Cache/Extracted/<Kind>      extracted toolsets and snapshots on this computer
# Machine Temporary/Work/Extracted/<Kind> extracted Linux trees on ext4
# Cache/ToolsetStages         downloaded Gentoo stages used to create toolsets
# Temporary/Sessions          builds and toolset environments
# Temporary/Work              writable toolset copies, work directories and unfinished extractions

def extracted_directory(kind: str, executor: Executor | None = None) -> str:
    executor = executor or LOCAL_EXECUTOR
    if executor.is_machine:
        # Linux trees can contain case-colliding paths (for example xt_CONNMARK.h and
        # xt_connmark.h). A macOS library volume is often case-insensitive, so extracted
        # trees must live on the machine's ext4 workspace instead of its shared Cache mount.
        return os.path.join(work_directory(executor), "Extracted", kind.capitalize())
    return os.path.join(executor.cache_directory(), "Extracted", kind.capitalize())

def distfiles_directory(executor: Executor | None = None) -> str:
    return os.path.join((executor or LOCAL_EXECUTOR).cache_directory(), "Distfiles")

def toolset_stages_directory() -> str:
    """Downloaded Gentoo stage archives reused when creating toolsets."""
    return os.path.join(LOCAL_EXECUTOR.cache_directory(), "ToolsetStages")

def sessions_directory(executor: Executor | None = None) -> str:
    return os.path.join((executor or LOCAL_EXECUTOR).temporary_directory(), "Sessions")

def work_directory(executor: Executor | None = None) -> str:
    return os.path.join((executor or LOCAL_EXECUTOR).temporary_directory(), "Work")

def clear_temporary_directory():
    """Removes data left in temporary directory of this computer by previous run of app (eg. after crash). Called
    when app starts, in background. Files owned by mapped ids are removed in namespace."""
    directory = LOCAL_EXECUTOR.temporary_directory()
    if not os.path.isdir(directory) or not os.listdir(directory):
        return
    print(f"Removing temporary files from {directory}")
    if rootless_unsupported_reason() is None:
        LOCAL_EXECUTOR.run_in_namespace(f"rm -rf -- {shlex.quote(directory)}/* {shlex.quote(directory)}/.[!.]*", print)
    else:
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            try:
                shutil.rmtree(path) if os.path.isdir(path) and not os.path.islink(path) else os.remove(path)
            except OSError as e:
                print(f"Failed to remove {path}: {e}")

def remove_stale_sessions(output_handler, process_holder: list | None = None, executor: Executor | None = None):
    """Sessions left by interrupted builds (only one build runs at a time). Sessions of toolset environments are kept."""
    directory = shlex.quote(sessions_directory(executor))
    script = f"""
set -- {directory}/build-*
[ -e "$1" ] || exit 0
echo "Removing $# unfinished build session(s)"
rm -rf -- "$@"
"""
    (executor or LOCAL_EXECUTOR).run_in_namespace(script, output_handler, process_holder)

def writable_squashfs_copy(squashfs_path: str, output_handler, process_holder: list | None = None, executor: Executor | None = None) -> str:
    """New extracted copy of squashfs file, for changing it and packing again. Remove it with remove_in_namespace."""
    executor = executor or LOCAL_EXECUTOR
    q = shlex.quote
    directory = work_directory(executor)
    path = os.path.join(directory, f"{os.path.basename(squashfs_path)}.{uuid.uuid4().hex}")
    output_handler(f"Extracting {os.path.basename(squashfs_path)}...")
    script = f"""
mkdir -p {q(directory)}
unsquashfs -n -d {q(path)} {q(squashfs_path)} 2>&1 | grep -v -e "create_inode: failed to create character device" -e "^$" | tail -5
[ -e {q(os.path.join(path, "usr"))} ] || {{ echo "Extraction failed"; rm -rf {q(path)}; exit 1; }}
"""
    if not executor.run_in_namespace(script, output_handler, process_holder):
        raise RuntimeError(f"Failed to extract {os.path.basename(squashfs_path)}")
    return path

def create_work_directory(prefix: str, executor: Executor | None = None) -> str:
    """Work directory in temporary directory, prefix can contain subdirectories (eg. 'toolsets/name/setup_')."""
    executor = executor or LOCAL_EXECUTOR
    *subdirectories, name_prefix = prefix.rstrip("/").split("/")
    parent = os.path.join(work_directory(executor), *subdirectories)
    path = os.path.join(parent, f"{name_prefix}{uuid.uuid4().hex[:8]}")
    executor.filesystem({"mkdir": [path]})
    return path

def squashfs_process(source_directory: str, output_file: str, executor: Executor | None = None) -> ExecutorProcess:
    """Packs directory to squashfs inside namespace, so files keep their owners. Output is like mksquashfs -percentage."""
    q = shlex.quote
    return (executor or LOCAL_EXECUTOR).start(f"exec mksquashfs {q(source_directory)} {q(output_file)} -quiet -percentage -noappend", namespace=True)

def extract_tarball(tarball: str, directory: str, output_handler, process_holder: list | None = None, executor: Executor | None = None) -> bool:
    """Extracts stage tarball with owners, special files and xattrs. Device nodes can't be created in namespace and are
    skipped (toolsets bind /dev). Prints PROGRESS: <0-1> lines."""
    script = f"""exec python3 - {shlex.quote(tarball)} {shlex.quote(directory)} <<'PYTHON'
import os, sys, tarfile
tarball, destination = sys.argv[1], os.path.abspath(sys.argv[2])
def stage_filter(member, dest_path):
    if member.ischr() or member.isblk():
        return None
    for path in [member.name] + ([member.linkname] if member.islnk() else []):
        target = os.path.normpath(os.path.join(destination, path))
        if os.path.isabs(path) or os.path.commonpath([target, destination]) != destination:
            raise tarfile.OutsideDestinationError(member, target)
    return member
with tarfile.open(tarball) as tar:
    members = tar.getmembers()
    total = sum(member.size for member in members) or 1
    done = 0
    for member in members:
        tar.extract(member, path=destination, filter=stage_filter)
        done += member.size
        print(f"PROGRESS: {{done / total}}", flush=True)
PYTHON
"""
    return (executor or LOCAL_EXECUTOR).run_in_namespace(script, output_handler, process_holder)

# ------------------------------------------------------------------------------
# Authorization.

class RootlessAuthorization:
    """Used instead of AuthorizationKeeper of root helper for actions running without root."""
    name = "Rootless"
    def retain(self):
        pass
    def release(self):
        pass

def authorize_toolset_action(callback, name: str | None = None, machine=None):
    """Like RootHelperClient.authorize_and_run, but without asking for root password when toolsets can run without
    root (on this computer, or in given machine). Callback is called in background thread, with RootlessAuthorization
    instead of authorization keeper."""
    if machine is not None or rootless_toolset_unsupported_reason() is None:
        threading.Thread(target=callback, args=(RootlessAuthorization(),), daemon=True).start()
        return
    from .root_helper_client import RootHelperClient
    if name is None:
        RootHelperClient.shared().authorize_and_run(callback=callback)
    else:
        RootHelperClient.shared().authorize_and_run(name=name, callback=callback)
