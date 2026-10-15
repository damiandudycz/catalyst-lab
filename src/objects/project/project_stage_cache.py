from __future__ import annotations
import os
from .project_stage_arguments import StageArgumentDetails
from .project_stage_automatic_option import StageAutomaticOption

# ------------------------------------------------------------------------------
# Package and kernel caches of stages.
# Each cache argument (pkgcache_path, kerncache_path) is one of:
# - Automatic (StageAutomaticOption): folder shared by stages with the same rel_type. Stages in one rel_type are built
#   with compatible settings, so they can reuse packages built by each other. Packages are in binary packages folder of
#   project (shared with other projects using it), kernels in project builds directory.
# - Manual (str): folder selected by user, path on host.
# - None: cache is disabled.

CACHE_ARGUMENTS = (StageArgumentDetails.pkgcache_path, StageArgumentDetails.kerncache_path)

# Folders of automatic caches inside project builds directory.
_automatic_cache_folders = {
    StageArgumentDetails.pkgcache_path: "packages",
    StageArgumentDetails.kerncache_path: "kerncache",
}

def is_automatic_cache(value) -> bool:
    # Stages created before caches had automatic option could inherit them from releng template. Template paths are
    # inside toolset and would be lost after build, so any automatic option means automatic cache.
    return isinstance(value, StageAutomaticOption)

def stage_cache_path(project_directory, stage, argument: StageArgumentDetails) -> str | None:
    """Host folder used as cache of stage, or None if cache is disabled or can't be determined yet (rel_type not set)."""
    from .project_build import project_builds_directory
    from .project_stage_value_resolver import resolve_stage_argument
    value = getattr(stage, argument.name, None)
    if isinstance(value, str):
        return os.path.abspath(os.path.expanduser(value)) if value.strip() else None
    if not is_automatic_cache(value):
        return None
    rel_type = resolve_stage_argument(project_directory, stage, StageArgumentDetails.rel_type.value)
    if not isinstance(rel_type, str) or not rel_type.strip("/ "):
        return None
    packages_directory = project_directory.get_packages_directory() if argument == StageArgumentDetails.pkgcache_path else None
    if packages_directory is not None:
        root = packages_directory.directory_path()
    else:
        root = os.path.join(project_builds_directory(project_directory), _automatic_cache_folders[argument])
    path = os.path.normpath(os.path.join(root, rel_type.strip("/")))
    return path if path.startswith(root + os.sep) else None

def display_path(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path
