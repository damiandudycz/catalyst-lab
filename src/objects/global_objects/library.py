from __future__ import annotations
import errno, json, os, shutil
from dataclasses import dataclass
from enum import Enum
from .repository import library_directory, store_library_directory, collapse_home

# ------------------------------------------------------------------------------
# Library is folder with all data of Catalyst Lab (toolsets, snapshots, projects, builds, machines) and its
# configuration (Config). It can be moved or copied to other place, or other library (new or existing) can be used.
# Locations stored in configuration that point into library are changed to new place. App is restarted after library
# changes, so nothing uses the old one.

LIBRARY_FOLDER_NAME = "CatalystLab" # Library made in chosen folder that contains other files.
_SKIPPED_FOLDERS = {"Temporary"} # Data of running operations, emptied when app starts.

class LibraryChange(Enum):
    MOVE = "move"
    COPY = "copy"
    NEW = "new"       # Empty library, set up by initial setup.
    SWITCH = "switch" # Existing library.

def is_library(path: str) -> bool:
    """Folder with configuration of Catalyst Lab."""
    config = os.path.join(path, "Config")
    try:
        return any(name.endswith(".json") for name in os.listdir(config))
    except OSError:
        return False

def _is_empty_folder(path: str) -> bool:
    """Doesn't exist or contains only hidden files of system (eg. .DS_Store)."""
    try:
        return all(name.startswith(".") for name in os.listdir(path))
    except FileNotFoundError:
        return True
    except OSError:
        return False

@dataclass
class LibraryTarget:
    path: str
    existing: bool # Contains library, it's used as it is.

def library_target(chosen: str) -> LibraryTarget:
    """Library at chosen folder: library in it, or folder itself when it's empty, or new folder in it. Raises
    ValueError with reason when it can't be used."""
    chosen = os.path.abspath(os.path.expanduser(chosen))
    current = library_directory()
    for path in (chosen, os.path.join(chosen, LIBRARY_FOLDER_NAME)):
        if _same_path(path, current):
            raise ValueError("This is the current library.")
        if is_library(path):
            return LibraryTarget(path, existing=True)
        if _is_empty_folder(path):
            if _inside(path, current) or _inside(current, path):
                raise ValueError("Library can't be kept inside the current library, or contain it.")
            return LibraryTarget(path, existing=False)
    raise ValueError(f"{collapse_home(chosen)} and {LIBRARY_FOLDER_NAME} folder in it contain other files. Choose an "
                     "empty folder or a folder with a library.")

def change_problem(target: LibraryTarget, change: LibraryChange) -> str | None:
    """Reason why library can't be changed this way, None when it can."""
    ancestor = _existing_ancestor(target.path)
    if not os.access(ancestor, os.W_OK):
        return f"{collapse_home(ancestor)} is not writable."
    if change not in (LibraryChange.MOVE, LibraryChange.COPY):
        return None
    from .lima import virtual_machines_supported, lima_home_too_long
    old = library_directory()
    if virtual_machines_supported() and os.path.isdir(os.path.join(old, "Machines")) and lima_home_too_long(os.path.join(target.path, "Machines")):
        return "Path is too long for virtual machines of library. Choose a folder with shorter path."
    if change == LibraryChange.COPY or not _same_device(old, ancestor):
        required = _library_size(old)
        free = shutil.disk_usage(ancestor).free
        if required > free:
            return f"There is not enough space: {_format_size(required)} is needed, {_format_size(free)} is free."
    return None

def change_library(target: LibraryTarget, change: LibraryChange, progress=lambda fraction, text: None) -> list[str]:
    """Makes target library used after restart, moving or copying current one when requested. Returns warnings
    (eg. files of moved library that couldn't be removed). Raises on failure, current library stays used then."""
    old = library_directory()
    warnings = []
    match change:
        case LibraryChange.SWITCH | LibraryChange.NEW:
            os.makedirs(target.path, exist_ok=True)
        case LibraryChange.MOVE if _same_device(old, _existing_ancestor(target.path)):
            progress(0, "Moving library")
            if os.path.isdir(target.path):
                _remove_hidden_files(target.path)
                os.rmdir(target.path)
            os.makedirs(os.path.dirname(target.path), exist_ok=True)
            os.rename(old, target.path)
            _rewrite_config_locations(target.path, old)
        case LibraryChange.MOVE | LibraryChange.COPY:
            _copy_library(old, target.path, progress)
            _rewrite_config_locations(target.path, old)
            if change == LibraryChange.MOVE:
                progress(1, "Removing old library")
                _remove_folder(old, warnings)
    store_library_directory(target.path)
    return warnings

# Copying:

def _copy_library(source: str, destination: str, progress):
    """Copies library (sparse files, like disks of machines, stay sparse). Destination is removed when it fails."""
    created = not os.path.exists(destination)
    total = max(_library_size(source), 1)
    copied = 0
    def advance(size: int, path: str):
        nonlocal copied
        copied += size
        progress(min(copied / total, 1), f"Copying {os.path.relpath(path, source)}")
    try:
        os.makedirs(destination, exist_ok=True)
        for directory, folders, files in os.walk(source):
            if directory == source:
                folders[:] = [folder for folder in folders if folder not in _SKIPPED_FOLDERS]
            target_directory = os.path.join(destination, os.path.relpath(directory, source))
            os.makedirs(target_directory, exist_ok=True)
            for name in folders + files:
                path = os.path.join(directory, name)
                target = os.path.join(target_directory, name)
                if os.path.islink(path):
                    os.symlink(os.readlink(path), target)
                    if name in folders:
                        folders.remove(name) # Not followed.
                elif name in files and os.path.isfile(path): # Sockets and other special files are skipped.
                    _copy_file(path, target, lambda size: advance(size, path))
            shutil.copystat(directory, target_directory)
    except BaseException:
        if created:
            shutil.rmtree(destination, ignore_errors=True)
        else:
            for name in os.listdir(destination):
                path = os.path.join(destination, name)
                shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) and not os.path.islink(path) else os.remove(path)
        raise

_CHUNK_SIZE = 8 * 1024 * 1024

def _copy_file(source: str, destination: str, advance):
    """Copies data of file, holes of sparse files are skipped (they stay holes in copy)."""
    with open(source, "rb") as input, open(destination, "wb") as output:
        size = os.fstat(input.fileno()).st_size
        for start, end in _data_ranges(input.fileno(), size):
            input.seek(start)
            output.seek(start)
            remaining = end - start
            while remaining > 0:
                chunk = input.read(min(_CHUNK_SIZE, remaining))
                if not chunk:
                    break
                output.write(chunk)
                remaining -= len(chunk)
                advance(len(chunk))
        output.truncate(size)
    shutil.copystat(source, destination)

def _data_ranges(descriptor: int, size: int) -> list[tuple[int, int]]:
    """Parts of file containing data (whole file when system can't find holes)."""
    if not hasattr(os, "SEEK_DATA"):
        return [(0, size)]
    ranges = []
    offset = 0
    try:
        while offset < size:
            try:
                start = os.lseek(descriptor, offset, os.SEEK_DATA)
            except OSError as e:
                if e.errno == errno.ENXIO: # No data after offset.
                    break
                raise
            end = os.lseek(descriptor, start, os.SEEK_HOLE)
            ranges.append((start, end))
            offset = end
    except OSError:
        return [(0, size)]
    return ranges

def _library_size(path: str) -> int:
    """Space used by files of library (sparse files count only their data)."""
    size = 0
    for directory, folders, files in os.walk(path):
        if directory == path:
            folders[:] = [folder for folder in folders if folder not in _SKIPPED_FOLDERS]
        for name in files:
            try:
                info = os.lstat(os.path.join(directory, name))
            except OSError:
                continue
            size += min(info.st_size, info.st_blocks * 512) if hasattr(info, "st_blocks") else info.st_size
    return size

# Locations:

def _rewrite_config_locations(library: str, old: str):
    """Locations in configuration of library pointing into old library point to the same places in new one (settings,
    machines). Locations elsewhere stay unchanged."""
    old_paths = {old, os.path.realpath(old)}
    def moved(value):
        if isinstance(value, dict):
            return {key: moved(item) for key, item in value.items()}
        if isinstance(value, list):
            return [moved(item) for item in value]
        if not isinstance(value, str) or not value.startswith(("/", "~")):
            return value
        expanded = os.path.expanduser(value)
        for path in old_paths:
            if expanded == path or expanded.startswith(path + os.sep):
                new_value = library + expanded[len(path):]
                return collapse_home(new_value) if value.startswith("~") else new_value
        return value
    config = os.path.join(library, "Config")
    for name in os.listdir(config):
        if not name.endswith(".json"):
            continue
        path = os.path.join(config, name)
        try:
            with open(path, encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, ValueError) as e:
            print(f"Failed to read {path}: {e}")
            continue
        changed = moved(data)
        if changed != data:
            with open(path, "w", encoding="utf-8") as file:
                json.dump(changed, file, indent=2)

# Helpers:

def _same_path(first: str, second: str) -> bool:
    return os.path.realpath(first) == os.path.realpath(second)

def _inside(path: str, folder: str) -> bool:
    return os.path.realpath(path).startswith(os.path.realpath(folder) + os.sep)

def _existing_ancestor(path: str) -> str:
    while not os.path.exists(path):
        path = os.path.dirname(path)
    return path

def _same_device(first: str, second: str) -> bool:
    return os.stat(first).st_dev == os.stat(second).st_dev

def _remove_folder(path: str, warnings: list[str]):
    """Removes folder, files that can't be removed are reported in warnings."""
    def failed(function, item, error):
        warnings.append(f"{item}: {error[1] if isinstance(error, tuple) else error}")
    import sys
    shutil.rmtree(path, **({"onexc": failed} if sys.version_info >= (3, 12) else {"onerror": failed}))

def _remove_hidden_files(path: str):
    for name in os.listdir(path):
        item = os.path.join(path, name)
        shutil.rmtree(item) if os.path.isdir(item) and not os.path.islink(item) else os.remove(item)

def _format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
