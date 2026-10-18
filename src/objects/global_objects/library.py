from __future__ import annotations
import errno, json, os, shutil
from dataclasses import dataclass
from enum import Enum
from .formatters import format_size
from .helper_functions import path_is_within
from .repository import library_directory, store_library_directory, collapse_home

# ------------------------------------------------------------------------------
# Library contains app data and object-local metadata. User preferences are stored separately.

LIBRARY_FOLDER_NAME = "CatalystLab" # Library made in chosen folder that contains other files.
_SKIPPED_FOLDERS = {"Temporary"} # Data of running operations, emptied when app starts.
LIBRARY_DIRECTORIES = (
    "Toolsets", "Snapshots", "Releng", "Overlays", "Projects", "Builds", "Packages", "Runners", "Cache",
    "Temporary",
)

class LibraryChange(Enum):
    MOVE = "move"
    COPY = "copy"
    NEW = "new"       # Empty library, set up by initial setup.
    SWITCH = "switch" # Existing library.

def is_library(path: str) -> bool:
    """A Library has the canonical object directories, not a central Config folder."""
    required = ("Toolsets", "Snapshots", "Releng", "Overlays", "Projects", "Packages")
    return os.path.isdir(path) and all(os.path.isdir(os.path.join(path, name)) for name in required)

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

def library_target(chosen: str, allow_current: bool = False) -> LibraryTarget:
    """Library at chosen folder: library in it, or folder itself when it's empty, or new folder in it. Raises
    ValueError with reason when it can't be used."""
    chosen = os.path.abspath(os.path.expanduser(chosen))
    current = library_directory()
    nested = os.path.join(chosen, LIBRARY_FOLDER_NAME)
    for path in (chosen, nested):
        if is_library(path):
            if _same_path(path, current) and not allow_current:
                raise ValueError("This is the current library.")
            return LibraryTarget(path, existing=True)
    if _is_empty_folder(chosen):
        target = chosen
    elif _is_empty_folder(nested):
        target = nested
    elif os.path.isdir(chosen) and not os.path.exists(nested):
        # The user selected a parent folder that already contains other files; keep the Library in its own subfolder.
        target = nested
    else:
        raise ValueError(f"{collapse_home(chosen)} and its {LIBRARY_FOLDER_NAME} subfolder contain other files, "
                         "but no Catalyst Lab Library was found.")
    if (path_is_within(target, current) or path_is_within(current, target)) and not (allow_current and _same_path(target, current)):
        raise ValueError("Library can't be kept inside the current library, or contain it.")
    return LibraryTarget(target, existing=False)

def create_library(path: str):
    """Create the standard Library folders after the user confirms the chosen location."""
    os.makedirs(path, exist_ok=True)
    for name in LIBRARY_DIRECTORIES:
        os.makedirs(os.path.join(path, name), exist_ok=True)

def change_problem(target: LibraryTarget, change: LibraryChange) -> str | None:
    """Reason why library can't be changed this way, None when it can."""
    ancestor = _existing_ancestor(target.path)
    if not os.access(ancestor, os.W_OK):
        return f"{collapse_home(ancestor)} is not writable."
    if change not in (LibraryChange.MOVE, LibraryChange.COPY):
        return None
    from .lima import virtual_machines_supported, lima_home_too_long
    old = library_directory()
    if virtual_machines_supported() and os.path.isdir(os.path.join(old, "Runners")) \
            and lima_home_too_long(os.path.join(target.path, "Runners")):
        return "Path is too long for VM runners of library. Choose a folder with shorter path."
    if change == LibraryChange.COPY or not _same_device(old, ancestor):
        required = _library_size(old)
        free = shutil.disk_usage(ancestor).free
        if required > free:
            return f"There is not enough space: {format_size(required)} is needed, {format_size(free)} is free."
    return None

def change_library(target: LibraryTarget, change: LibraryChange, progress=lambda fraction, text: None) -> list[str]:
    """Makes target library used after restart, moving or copying current one when requested. Returns warnings
    (eg. files of moved library that couldn't be removed). Raises on failure, current library stays used then."""
    old = library_directory()
    warnings = []
    match change:
        case LibraryChange.SWITCH | LibraryChange.NEW:
            # A switched Library may be missing newer standard folders, and a newly
            # created Library needs the full layout before repositories reload.
            create_library(target.path)
        case LibraryChange.MOVE if _same_device(old, _existing_ancestor(target.path)):
            progress(0, "Moving library")
            if os.path.isdir(target.path):
                _remove_hidden_files(target.path)
                os.rmdir(target.path)
            os.makedirs(os.path.dirname(target.path), exist_ok=True)
            os.rename(old, target.path)
            _rewrite_object_paths(target.path, old)
        case LibraryChange.MOVE | LibraryChange.COPY:
            _copy_library(old, target.path, progress)
            _rewrite_object_paths(target.path, old)
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
            _remove_hidden_files(destination)
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

def _rewrite_object_paths(library: str, old: str):
    """Update object metadata paths when a Library is moved; external paths stay unchanged."""
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
    metadata_names = {"catalystlab.json", "metadata.json"}
    for directory, folders, files in os.walk(library):
        folders[:] = [folder for folder in folders if folder != ".git"]
        candidates = [os.path.join(directory, name) for name in files
                     if name in metadata_names or name.endswith(".sqfs.json")]
        # Git metadata is intentionally kept out of the working tree.
        git_dir = os.path.join(directory, ".git")
        if os.path.isdir(git_dir):
            candidate = os.path.join(git_dir, "catalystlab.json")
            if os.path.isfile(candidate):
                candidates.append(candidate)
        for path in candidates:
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
