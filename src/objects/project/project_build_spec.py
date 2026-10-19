from __future__ import annotations
import os, re, shutil, uuid
from dataclasses import dataclass, field
from typing import Callable
from .repository import Repository
from .architecture import Architecture
from .snapshot import PortageProfile
from .project_stage import load_catalyst_stage_arguments_details
from .project_stage_arguments import StageArgumentDetails
from .project_stage_automatic_option import StageAutomaticOption
from .project_stage_compression_mode import StageCompressionMode
from .project_stage_value_resolver import (
    resolve_stage_argument, load_stage_releng_template_values, stage_argument_uses_option, split_additions, UNRESOLVED,
)
from .project_stage_portage_confdir import (
    StagePortageConfdirSource, portage_confdir_sources, releng_portage_variant, parent_overlay_stages,
    stage_overlay_path, is_emulated_build, root_overlay_values, root_overlay_folders
)

# ------------------------------------------------------------------------------
# Generating catalyst spec files for stages.

# Arguments describing stage only in CatalystLab, not passed to catalyst.
_virtual_arguments = {StageArgumentDetails.name.value, StageArgumentDetails.parent.value, StageArgumentDetails.releng_template.value}

@dataclass
class StageSpecContext:
    """Values known only when building."""
    timestamp: str          # Replaces @TIMESTAMP@.
    source_subpath: str     # Seed, relative to catalyst builds directory.
    portage_confdir: str | None # Generated portage configuration, as seen by catalyst.
    toolset: object | None = None # Toolset selected for this build.
    cache_paths: dict = field(default_factory=dict) # Enabled caches (pkgcache_path, kerncache_path), as seen by catalyst.
    root_overlay: str | None = None # Generated root overlay, as seen by catalyst.
    iso_overlay: str | None = None # Generated ISO overlay (livecd/overlay), as seen by catalyst.
    snapshot: object | None = None # Snapshot selected for build, project snapshot when not set.
    path_mapper: Callable[[str], str] | None = None # Path of host folder as seen by catalyst (project, releng, overlays).
    rosetta_amd64: bool = False # Rosetta binfmt handler is active in the build machine.

def snapshot_treeish(project_directory, snapshot=None) -> str | None:
    """Catalyst finds snapshot as snapshots/gentoo-<treeish>.sqfs. Snapshot of project, unless other is given."""
    snapshot = snapshot or project_directory.get_snapshot()
    if snapshot is None:
        return None
    match = re.match(r"^gentoo-(.+)\.sqfs$", snapshot.filename)
    return match.group(1) if match else None

def generate_stage_spec(project_directory, stage, context: StageSpecContext) -> str:
    """Spec file contents, with automatic options resolved and placeholders replaced. Raises if required value is missing."""
    # Defaults are normally refreshed when the stage details are opened. Build
    # generation must also work for projects/stages that have never been opened
    # in that view (or whose defaults depend on a newly selected build snapshot).
    from .project_stage import refresh_default_stage_arguments
    refresh_default_stage_arguments(project_directory, stage, toolset=context.toolset, snapshot=context.snapshot)
    arguments = load_catalyst_stage_arguments_details(toolset=context.toolset, target_name=stage.target)
    releng_directory = project_directory.get_releng_directory()
    mapped = context.path_mapper or (lambda path: path)
    placeholders = {
        "@TIMESTAMP@": context.timestamp,
        "@REPO_DIR@": mapped(releng_directory.directory_path()) if releng_directory else "",
        # Files kept in project (eg. added by project templates), available in toolset (paths without whitespace).
        "@PROJECT_DIR@": mapped(project_directory.directory_path()),
        "@STAGE_DIR@": mapped(project_directory.stage_directory_path(name=stage.name)),
        "@TREEISH@": snapshot_treeish(project_directory, context.snapshot) or "",
    }
    lines, missing = [], []
    for name, argument in arguments.items():
        if name in _virtual_arguments:
            continue
        value = _argument_value(project_directory, stage, name, argument, context)
        line = _format_spec_line(name, value, mapped, placeholders)
        if line is None:
            if argument.required:
                missing.append(argument.display_name)
            continue
        lines.append(line)
    # Settings of kernels (boot/kernel/<name>/...), own of stage or from its releng template.
    if StageArgumentDetails.boot_kernel.value in arguments:
        from .project_stage_kernels import kernel_spec_values
        for name, value in kernel_spec_values(project_directory, stage):
            if line := _format_spec_line(name, value, mapped, placeholders):
                lines.append(line)
    if missing:
        raise RuntimeError(f"Missing required values: {', '.join(missing)}")
    return "\n".join(lines) + "\n"

def _format_spec_line(name: str, value, path_mapper, placeholders: dict[str, str]) -> str | None:
    if (text := _format_value(value, path_mapper)) is None:
        return None
    for placeholder, replacement in placeholders.items():
        text = text.replace(placeholder, replacement)
    return f"{name}: {text}"

def _argument_value(project_directory, stage, name: str, argument, context: StageSpecContext):
    if argument.details == StageArgumentDetails.interpreter:
        return _interpreter_argument_value(project_directory, stage, argument, context)
    match argument.details:
        case StageArgumentDetails.target:
            return stage.target.replace("_", "-")
        case StageArgumentDetails.source_subpath:
            return context.source_subpath
        case StageArgumentDetails.snapshot_treeish:
            return snapshot_treeish(project_directory, context.snapshot)
        case StageArgumentDetails.portage_confdir:
            return context.portage_confdir
        case StageArgumentDetails.pkgcache_path | StageArgumentDetails.kerncache_path:
            return context.cache_paths.get(argument.details)
        case StageArgumentDetails.stage4_root_overlay | StageArgumentDetails.livecd_root_overlay:
            return context.root_overlay
        case StageArgumentDetails.livecd_overlay:
            return context.iso_overlay
    value = resolve_stage_argument(project_directory, stage, name)
    if (value is None and argument.details == StageArgumentDetails.interpreter
            and argument.attribute_name not in getattr(stage, "_explicit_none_arguments", set())):
        # An omitted interpreter means CatalystLab should choose one for an emulated target. Keep the project setting
        # unset; an explicitly stored None remains a deliberate override.
        value = _automatic_value(project_directory, argument, context)
    if value is UNRESOLVED and stage_argument_uses_option(
            project_directory, stage, argument.attribute_name, {StageAutomaticOption.GENERATE_AUTOMATICALLY}):
        value = _automatic_value(project_directory, argument, context)
    return value

def _interpreter_argument_value(project_directory, stage, argument, context: StageSpecContext):
    """Resolve automatic, inherited, and user-supplied interpreter entries together."""
    attribute = argument.attribute_name
    stored = getattr(stage, attribute, None)
    if stored is None:
        if attribute in getattr(stage, "_explicit_none_arguments", set()):
            return None
        return _automatic_value(project_directory, argument, context)

    if isinstance(stored, StageAutomaticOption):
        options, own = [stored], []
    elif isinstance(stored, list):
        if (combined := split_additions(stored)) is not None:
            options, own = combined
        elif stored and all(isinstance(item, StageAutomaticOption) for item in stored):
            options, own = stored, []
        else:
            options, own = [], stored
    else:
        return stored

    # Rosetta already runs native amd64 programs, so omit inherited/generated QEMU interpreters. Keep any explicit
    # custom entries the user added alongside Automatic or template inheritance.
    skip_emulated = project_directory.get_architecture() == Architecture.amd64 and context.rosetta_amd64
    values = []
    for option in options:
        if option in (StageAutomaticOption.GENERATE_AUTOMATICALLY, StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE) \
                and skip_emulated:
            continue
        value = (_automatic_value(project_directory, argument, context)
                 if option == StageAutomaticOption.GENERATE_AUTOMATICALLY else
                 resolve_stage_argument(project_directory, stage, argument.name, option=option))
        if value is UNRESOLVED or value is None:
            continue
        values.extend(value if isinstance(value, list) else [value])
    values.extend(own if isinstance(own, list) else [own])
    return list(dict.fromkeys(value for value in values if value not in (None, "", []))) or None

def _automatic_value(project_directory, argument, context: StageSpecContext):
    """Automatic values determined when building. None means catalyst default."""
    match argument.details:
        case StageArgumentDetails.interpreter:
            architecture = project_directory.get_architecture()
            if architecture == Architecture.amd64 and context.rosetta_amd64:
                return None # Lima's Rosetta binfmt handler runs amd64 binaries without Catalyst copying QEMU in.
            return architecture.qemu_user_binary() if architecture and is_emulated_build(architecture) else None
    return None

def _format_value(value, path_mapper) -> str | None:
    if value is None or value is UNRESOLVED or isinstance(value, StageAutomaticOption) or value == "" or value == []:
        return None
    if isinstance(value, list):
        items = [text for item in value if (text := _format_value(item, path_mapper)) is not None]
        return ("\n\t" + "\n\t".join(items)) if len(items) > 1 else (items[0] if items else None)
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, PortageProfile):
        return value.path if value.repo == "gentoo" else f"{value.repo}:{value.path}"
    if isinstance(value, StageCompressionMode):
        return value.name
    if isinstance(value, uuid.UUID):
        # Repos are stored by overlay id, catalyst needs their location.
        overlay = next((item for item in Repository.OverlayDirectory.value if item.id == value), None)
        return path_mapper(overlay.directory_path()) if overlay else None
    return str(value)

# ------------------------------------------------------------------------------
# Portage configuration:

def generate_portage_confdir(project_directory, stage, destination: str) -> bool:
    """Combines selected portage configuration sources into destination folder. Later sources overwrite files of
    earlier ones. Returns false if stage doesn't use any source."""
    sources = portage_confdir_sources(stage)
    folders = []
    if StagePortageConfdirSource.RELENG in sources:
        releng_directory = project_directory.get_releng_directory()
        variant = releng_portage_variant(project_directory, stage)
        if releng_directory and variant:
            folders.append(os.path.join(releng_directory.directory_path(), "releases", "portage", variant))
    if StagePortageConfdirSource.PARENT_OVERLAY in sources:
        folders += [stage_overlay_path(project_directory, item) for item in parent_overlay_stages(project_directory, stage)]
    if StagePortageConfdirSource.STAGE_OVERLAY in sources:
        folders.append(stage_overlay_path(project_directory, stage))
    if not sources:
        return False
    _copy_folders(folders, destination)
    return True

# ------------------------------------------------------------------------------
# Root overlay:

def generate_root_overlay(project_directory, stage, destination: str, argument: StageArgumentDetails | None = None) -> bool:
    """Combines selected root overlay sources into destination folder (Releng overlays and the stage overlay).
    Later sources overwrite earlier ones. Parent overlays are already in the parent's built filesystem. Returns false
    if stage doesn't use root overlay or none of its folders exists."""
    if not root_overlay_values(project_directory, stage, argument):
        return False
    folders = [folder for folder in root_overlay_folders(project_directory, stage, argument) if os.path.isdir(folder)]
    if not folders:
        return False
    _copy_folders(folders, destination)
    return True

def _copy_folders(folders: list[str], destination: str) -> None:
    """Merge folders in order; later folders overwrite earlier files."""
    os.makedirs(destination, exist_ok=True)
    for folder in folders:
        if os.path.isdir(folder):
            shutil.copytree(folder, destination, dirs_exist_ok=True, symlinks=True)

# ------------------------------------------------------------------------------
# Seeds of root stages:

def seed_name_prefix(project_directory, stage) -> str:
    """Name of stage3 used as seed for stage without parent, eg. stage3-arm64-openrc. Taken from releng template
    seed if possible, otherwise guessed from stage name and version stamp."""
    template_values = load_stage_releng_template_values(project_directory=project_directory, stage=stage) or {}
    template_seed = template_values.get(StageArgumentDetails.source_subpath.value)
    if isinstance(template_seed, str):
        name = os.path.basename(template_seed)
        name = re.sub(r"(\.tar\.\w+)$", "", name)
        name = re.sub(r"-(latest|@TIMESTAMP@)$", "", name)
        if name.startswith("stage3-"):
            return name
    architecture = project_directory.get_architecture()
    hints = f"{stage.name} {getattr(stage, StageArgumentDetails.version_stamp.name, '') or ''}"
    init_system = "systemd" if "systemd" in str(hints) else "openrc"
    return f"stage3-{architecture.value}-{init_system}"

def select_seed_url(urls: list, prefix: str):
    """Picks stage3 matching prefix exactly (stage3-arm64-openrc-<timestamp>.tar.xz, not -desktop- variants)."""
    pattern = re.compile(rf"^{re.escape(prefix)}-\d{{8}}T\d{{6}}Z\.tar\.\w+$")
    return next((url for url in urls if pattern.match(os.path.basename(url.path))), None)
