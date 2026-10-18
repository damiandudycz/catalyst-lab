from .root_function import root_function
from catalystlab.formatters import format_size
import os, re, shutil, signal, subprocess, tarfile, tempfile, threading, uuid
from datetime import datetime
from email.utils import parsedate_to_datetime

# ------------------------------------------------------------------------------
# Global helper functions:
# ------------------------------------------------------------------------------

def path_is_within(path: str, directory: str) -> bool:
    """Whether a path resolves to the directory or one of its children."""
    path, directory = os.path.realpath(path), os.path.realpath(directory)
    try:
        return os.path.commonpath((path, directory)) == directory
    except ValueError:  # Paths on different drives.
        return False

@root_function
def create_temp_workdir(prefix: str) -> str:
    """Creates temp directory in /var/tmp/catalystlab, owned by the user.
    Supports subdirectories inside prefix, e.g. 'toolset/tmp_'."""
    base_dir = "/var/tmp/catalystlab"
    # Split prefix into path components
    if '/' in prefix:
        *subdirs, final_prefix = prefix.rstrip('/').split('/')
        dir_path = os.path.join(base_dir, *subdirs)
    else:
        final_prefix = prefix
        dir_path = base_dir
    os.makedirs(dir_path, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix=final_prefix, dir=dir_path)
    uid = RootHelperServer.shared().uid
    os.chown(temp_dir, uid, uid)
    return temp_dir

@root_function
def delete_temp_workdir(path: str) -> bool:
    try:
        resolved_path = os.path.realpath(path)
        # Ensure we're operating strictly inside the expected directory
        if not (resolved_path.startswith("/var/tmp/catalystlab/") and resolved_path != "/var/tmp/catalystlab"):
            raise ValueError(f"Refusing to delete path outside /var/tmp/catalystlab: {resolved_path}")
        if not os.path.isdir(resolved_path):
            raise FileNotFoundError(f"Path does not exist or is not a directory: {resolved_path}")
        # TODO: Detect if path contains any bindings and raise if it does. Note: os.path.ismount will probably not work for this.
        shutil.rmtree(path)
        print(f"Successfully deleted the directory: {path}")
        return True
    except Exception as e:
        print(f"Failed to delete directory {path}: {e}")
        return False

@root_function
def mount_squashfs(squashfs_path: str, prefix: str) -> str:
    """Creates tmp directory and extracts squashfs file to it"""
    mount_point = create_temp_workdir(prefix=prefix)
    subprocess.run(['unsquashfs', '-f', '-d', mount_point, squashfs_path], check=True)
    return mount_point

@root_function
def cached_squashfs_copy(squashfs_path: str, cache_path: str, prefix: str, check_path: str = "usr") -> str:
    """Keep one extracted image in Cache, then create a writable per-operation copy in Temporary."""
    source = os.stat(squashfs_path)
    stamp = f"{source.st_size}:{source.st_mtime_ns}"
    stamp_path = cache_path + ".stamp"
    try:
        with open(stamp_path, encoding="utf-8") as file:
            cached_stamp = file.read()
    except OSError:
        cached_stamp = None
    if not (os.path.isdir(cache_path) and os.path.exists(os.path.join(cache_path, check_path))
            and cached_stamp == stamp):
        parent = os.path.dirname(cache_path)
        os.makedirs(parent, exist_ok=True)
        staging = cache_path + f".{uuid.uuid4().hex}.tmp"
        try:
            subprocess.run(["unsquashfs", "-f", "-d", staging, squashfs_path], check=True)
            if not os.path.exists(os.path.join(staging, check_path)):
                raise RuntimeError(f"Extraction of {os.path.basename(squashfs_path)} is missing {check_path}")
            if os.path.isdir(cache_path):
                shutil.rmtree(cache_path)
            elif os.path.lexists(cache_path):
                os.unlink(cache_path)
            os.replace(staging, cache_path)
            with open(stamp_path + ".tmp", "w", encoding="utf-8") as file:
                file.write(stamp)
            os.replace(stamp_path + ".tmp", stamp_path)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    temporary = create_temp_workdir(prefix)
    try:
        subprocess.run(["cp", "-a", "--", cache_path + "/.", temporary + "/"], check=True)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return temporary

@root_function
def umount_squashfs(mount_point: str):
    delete_temp_workdir(path=mount_point)

@root_function
def loop_mount_squashfs(squashfs_path: str, prefix: str) -> str:
    """Mounts squashfs file read only in new tmp directory. Much faster than extracting it, files are decompressed
    only when read. Changes need to be stored using overlays on top of it."""
    mount_point = create_temp_workdir(prefix=prefix)
    try:
        subprocess.run(['mount', '-t', 'squashfs', '-o', 'ro,loop', squashfs_path, mount_point], check=True)
    except Exception:
        os.rmdir(mount_point)
        raise
    return mount_point

@root_function
def loop_umount_squashfs(mount_point: str):
    """Unmounts squashfs mounted with loop_mount_squashfs and removes its mount point. Mount point is never deleted
    recursively, so if unmounting fails, contents of squashfs are not touched."""
    resolved_path = os.path.realpath(mount_point)
    if not resolved_path.startswith("/var/tmp/catalystlab/"):
        raise ValueError(f"Refusing to unmount path outside /var/tmp/catalystlab: {resolved_path}")
    if os.path.ismount(resolved_path):
        subprocess.run(['umount', resolved_path], check=True)
    os.rmdir(resolved_path)

@root_function
def extract(tarball: str, directory: str):
    """Extract an .xz tarball as root to preserve special files and ownership."""
    _cancel_event = threading.Event()
    def handle_sigterm(signum, frame):
        _cancel_event.set()
    signal.signal(signal.SIGTERM, handle_sigterm)

    destination = os.path.abspath(directory)
    def stage_filter(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo:
        # Stage tarballs contain device nodes, absolute symlinks (resolved later inside chroot),
        # ownership and setuid bits, which the default 'data' filter (Python 3.14+) rejects or strips.
        # Keep them all, but don't allow member paths or hardlinks escaping the destination.
        paths = [member.name] + ([member.linkname] if member.islnk() else [])
        for path in paths:
            target = os.path.normpath(os.path.join(destination, path))
            if os.path.isabs(path) or os.path.commonpath([target, destination]) != destination:
                raise tarfile.OutsideDestinationError(member, target)
        return member
    with tarfile.open(tarball, mode='r:xz') as tar:
        members = tar.getmembers()
        total_size = sum(member.size for member in members)
        extracted_size = 0
        for member in members:
            if _cancel_event.is_set():
                return
            tar.extract(member, path=directory, filter=stage_filter)
            extracted_size += member.size
            progress = extracted_size / total_size if total_size else 0
            # This print must stay, it is used to receive progress by step implementation.
            print(f"PROGRESS: {progress}", flush=True)

def create_work_directory(prefix: str, rootless: bool) -> str:
    """Work directory for toolset files. Rootless ones are in temporary directory, owned by user and mapped ids."""
    if rootless:
        from .rootless import create_work_directory as create_rootless_work_directory
        return create_rootless_work_directory(prefix=prefix)
    return create_temp_workdir(prefix=prefix)

def delete_work_directory(path: str):
    """Deletes directory created with create_work_directory."""
    from .rootless import is_rootless_path, remove_in_namespace
    if is_rootless_path(path):
        remove_in_namespace([path], print)
    else:
        delete_temp_workdir(path=path)

def create_squashfs(source_directory: str, output_file: str) -> subprocess.Popen:
    """Note: Runs as separate process, so need to wait for it to finish when called.
    Rootless toolset files (owned by mapped ids) are packed inside user namespace, to keep their owners."""
    from .rootless import is_rootless_path, squashfs_process
    if is_rootless_path(source_directory):
        return squashfs_process(source_directory=source_directory, output_file=output_file)
    command = ['mksquashfs', source_directory, output_file, '-quiet', '-percentage']
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    return process

def get_file_size_string(path: str) -> str | None:
    try:
        size = os.path.getsize(path)
        return f"{size:.1f} B" if size < 1024 else format_size(size)
    except Exception as e:
        print(e)
        return None

def parse_strict_rfc_datetime(s: str) -> datetime:
    value = s.strip()
    if not re.fullmatch(r"[A-Za-z]{3}, \d{1,2} [A-Za-z]{3} \d{4} \d{2}:\d{2}:\d{2} [+-]\d{4}", value):
        raise ValueError("Invalid RFC datetime")
    return parsedate_to_datetime(value)

