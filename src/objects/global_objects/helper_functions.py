from .root_function import root_function
import subprocess, os, re
from datetime import datetime, timezone, timedelta

# ------------------------------------------------------------------------------
# Global helper functions:
# ------------------------------------------------------------------------------

@root_function
def create_temp_workdir(prefix: str) -> str:
    """Creates temp directory in /var/tmp/catalystlab, owned by the user.
    Supports subdirectories inside prefix, e.g. 'toolset/tmp_'."""
    import tempfile
    import os
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
    import os
    import shutil
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
    import os
    import subprocess
    """Creates tmp directory and extracts squashfs file to it"""
    mount_point = create_temp_workdir(prefix=prefix)
    subprocess.run(['unsquashfs', '-f', '-d', mount_point, squashfs_path], check=True)
    return mount_point

@root_function
def umount_squashfs(mount_point: str):
    delete_temp_workdir(path=mount_point)

@root_function
def loop_mount_squashfs(squashfs_path: str, prefix: str) -> str:
    """Mounts squashfs file read only in new tmp directory. Much faster than extracting it, files are decompressed
    only when read. Changes need to be stored using overlays on top of it."""
    import subprocess
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
    import subprocess
    resolved_path = os.path.realpath(mount_point)
    if not resolved_path.startswith("/var/tmp/catalystlab/"):
        raise ValueError(f"Refusing to unmount path outside /var/tmp/catalystlab: {resolved_path}")
    if os.path.ismount(resolved_path):
        subprocess.run(['umount', resolved_path], check=True)
    os.rmdir(resolved_path)

@root_function
def extract(tarball: str, directory: str):
    import tarfile
    import signal
    import threading
    _cancel_event = threading.Event()
    def handle_sigterm(signum, frame):
        _cancel_event.set()
    signal.signal(signal.SIGTERM, handle_sigterm)

    """Extracts an .xz tarball as root to preserve special files and ownership."""
    import os
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
        total_size = sum(member.size for member in tar.getmembers())
        extracted_size = 0
        for member in tar.getmembers():
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
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"
    except Exception as e:
        print(e)
        return None

def parse_strict_rfc_datetime(s: str) -> datetime:
    import locale
    match = re.search(r'([+-]\d{4})$', s.strip())
    if not match:
        raise ValueError("No timezone offset found")
    tz_str = match.group(1)
    s_no_tz = s.replace(tz_str, '').strip()
    # Save the current locale
    current_locale = locale.setlocale(locale.LC_TIME)
    try:
        # Temporarily set locale to "C" for strptime to parse English weekday/month
        locale.setlocale(locale.LC_TIME, "C")
        dt = datetime.strptime(s_no_tz, "%a, %d %b %Y %H:%M:%S")
    finally:
        # Restore the original locale no matter what happens
        locale.setlocale(locale.LC_TIME, current_locale)
    # Convert timezone offset to tzinfo
    hours = int(tz_str[1:3])
    minutes = int(tz_str[3:5])
    delta = timedelta(hours=hours, minutes=minutes)
    if tz_str.startswith('-'):
        delta = -delta
    return dt.replace(tzinfo=timezone(delta))

