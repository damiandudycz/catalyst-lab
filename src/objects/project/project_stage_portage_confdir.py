from __future__ import annotations
import filecmp, os
from enum import Enum
from .architecture import Architecture
from .project_stage_arguments import StageArgumentDetails, StageArgumentOption

# ------------------------------------------------------------------------------
# portage_confdir is generated when building stage, by combining selected sources
# in fixed order (later ones overwrite files of earlier ones):
# releng portage configuration -> parent overlays -> stage overlay.

class StagePortageConfdirSource(Enum):
    RELENG = "releng"
    PARENT_OVERLAY = "parent_overlay"
    STAGE_OVERLAY = "stage_overlay"

    @property
    def display_name(self) -> str:
        match self:
            case StagePortageConfdirSource.RELENG: return "Releng configuration"
            case StagePortageConfdirSource.PARENT_OVERLAY: return "Parent overlay"
            case StagePortageConfdirSource.STAGE_OVERLAY: return "Stage overlay"

# Order in which sources are combined, and in which they are displayed.
PORTAGE_CONFDIR_SOURCES_ORDER = [
    StagePortageConfdirSource.RELENG,
    StagePortageConfdirSource.PARENT_OVERLAY,
    StagePortageConfdirSource.STAGE_OVERLAY,
]

def default_portage_confdir_sources(has_parent: bool) -> list[StagePortageConfdirSource]:
    """Sources used by new stages. Stage overlay is not enabled by default, to not create empty folders."""
    return [StagePortageConfdirSource.RELENG] + ([StagePortageConfdirSource.PARENT_OVERLAY] if has_parent else [])

def portage_confdir_sources(stage) -> list[StagePortageConfdirSource]:
    value = getattr(stage, StageArgumentDetails.portage_confdir.name, None)
    values = value if isinstance(value, list) else [value]
    return [source for source in PORTAGE_CONFDIR_SOURCES_ORDER if source in values]

# ------------------------------------------------------------------------------
# Overlays:

def stage_overlay_path(project_directory, stage) -> str:
    """Folder with stage portage configuration overlay, kept in stage directory of project."""
    return os.path.join(project_directory.stage_directory_path(name=stage.name), "portage")

def count_files(path: str) -> int:
    """Number of files in folder, including subfolders."""
    return sum(len(files) for _, _, files in os.walk(path))

def _parent_stage(project_directory, stage):
    parent_id = getattr(stage, StageArgumentDetails.parent.name, None)
    return next((item for item in project_directory.stages if item.id == parent_id), None) if parent_id else None

def overlay_stages(project_directory, stage, _visited: frozenset = frozenset()) -> list:
    """Stages whose overlays are used by stage, in order they are applied. Follows parent overlay through the chain
    and stops at stage that doesn't use parent overlay."""
    if stage.id in _visited:
        return [] # Parents loop.
    sources = portage_confdir_sources(stage)
    stages = []
    if StagePortageConfdirSource.PARENT_OVERLAY in sources:
        stages += parent_overlay_stages(project_directory, stage, _visited | {stage.id})
    if StagePortageConfdirSource.STAGE_OVERLAY in sources:
        stages.append(stage)
    return stages

def parent_overlay_stages(project_directory, stage, _visited: frozenset = frozenset()) -> list:
    """Stages whose overlays are inherited from parent (all overlays used by parent)."""
    parent = _parent_stage(project_directory, stage)
    return overlay_stages(project_directory, parent, _visited) if parent else []

# ------------------------------------------------------------------------------
# Releng configuration variant (releng/releases/portage/<variant>):

# Base variant for targets. Other targets use stages configuration.
_releng_portage_target_variants: dict[str, str] = {
    "livecd_stage1": "isos",
    "livecd_stage2": "isos",
    "diskimage_stage1": "diskimage",
    "diskimage_stage2": "diskimage",
}
# Architecture specific variants of base variant.
_releng_portage_arch_variants: dict[tuple[str, Architecture], str] = {
    ("isos", Architecture.x86): "isos-x86",
}
# Variants used when building under emulation (qemu).
_RELENG_PORTAGE_EMULATION_SUFFIX = "-qemu"
# Architectures that host can build natively, other than its own.
_natively_supported_architectures: dict[Architecture, set[Architecture]] = {
    Architecture.amd64: {Architecture.x86},
}

def is_emulated_build(architecture: Architecture) -> bool:
    """Stages of other architectures than host (and ones host supports natively) are built under qemu."""
    return architecture != Architecture.HOST and architecture not in _natively_supported_architectures.get(Architecture.HOST, set())

def releng_portage_variant(project_directory, stage) -> str | None:
    """Name of releng portage configuration directory matching stage target, architecture and emulation. Variants
    missing in used releng directory fall back to more generic ones."""
    architecture = project_directory.get_architecture()
    if architecture is None or not stage.target:
        return None
    base = _releng_portage_target_variants.get(stage.target, "stages")
    variant = _releng_portage_arch_variants.get((base, architecture), base)
    candidates = [variant, base]
    if is_emulated_build(architecture):
        candidates = [name + _RELENG_PORTAGE_EMULATION_SUFFIX for name in candidates] + candidates
    releng_directory = project_directory.get_releng_directory()
    if releng_directory is not None:
        portage_path = os.path.join(releng_directory.directory_path(), "releases", "portage")
        existing = next((name for name in candidates if os.path.isdir(os.path.join(portage_path, name))), None)
        if existing:
            return existing
    return candidates[0]

# ------------------------------------------------------------------------------
# Options displayed in UI:

def portage_confdir_options(project_directory, stage) -> list[StageArgumentOption]:
    argument = StageArgumentDetails.portage_confdir
    # Releng:
    variant = releng_portage_variant(project_directory, stage)
    releng = StageArgumentOption(
        raw=StagePortageConfdirSource.RELENG, value=StagePortageConfdirSource.RELENG, argument=argument,
        display=StagePortageConfdirSource.RELENG.display_name,
        subtitle=f"releases/portage/{variant}" if variant else "Releng configuration matching stage"
    )
    # Parent overlay:
    has_parent = _parent_stage(project_directory, stage) is not None
    parent_stages = parent_overlay_stages(project_directory, stage)
    parent_files = sum(count_files(stage_overlay_path(project_directory, item)) for item in parent_stages)
    parent_subtitle = (
        "Overlays used by parent stage" if not has_parent else
        f"{_files_text(parent_files)} from {', '.join(item.name for item in parent_stages)}" if parent_stages else
        "Parent stages don't use overlays"
    )
    parent = StageArgumentOption(
        raw=StagePortageConfdirSource.PARENT_OVERLAY, value=StagePortageConfdirSource.PARENT_OVERLAY, argument=argument,
        display=StagePortageConfdirSource.PARENT_OVERLAY.display_name, subtitle=parent_subtitle,
        unsupported=not has_parent
    )
    # Stage overlay:
    stage_files = count_files(stage_overlay_path(project_directory, stage))
    own = StageArgumentOption(
        raw=StagePortageConfdirSource.STAGE_OVERLAY, value=StagePortageConfdirSource.STAGE_OVERLAY, argument=argument,
        display=StagePortageConfdirSource.STAGE_OVERLAY.display_name,
        subtitle=f"{_files_text(stage_files)}, overwrite releng and parent files"
    )
    return [releng, parent, own]

def _files_text(count: int) -> str:
    return "1 file" if count == 1 else f"{count} files"

# ------------------------------------------------------------------------------
# File overlays: root overlay (stage4/root_overlay, livecd/root_overlay, copied into stage filesystem) and ISO overlay
# (livecd/overlay, copied into ISO next to live system). They use the same sources as portage_confdir, combined when
# building in the same order: releng template overlays -> parent overlays -> stage overlay.

ROOT_OVERLAY_ARGUMENTS = (StageArgumentDetails.stage4_root_overlay, StageArgumentDetails.livecd_root_overlay)
FILE_OVERLAY_ARGUMENTS = ROOT_OVERLAY_ARGUMENTS + (StageArgumentDetails.livecd_overlay,)

# Folders of overlays in stage directory. ISO overlay folder is the one used by templates before it had sources.
_FILE_OVERLAY_FOLDERS = {StageArgumentDetails.livecd_overlay: "overlay"}

def root_overlay_argument(stage) -> StageArgumentDetails | None:
    """Root overlay argument of stage target."""
    target = (getattr(stage, "target", None) or "").replace("-", "_")
    if target == "stage4":
        return StageArgumentDetails.stage4_root_overlay
    if target.startswith("livecd"):
        return StageArgumentDetails.livecd_root_overlay
    return None

def stage_root_overlay_path(project_directory, stage, argument: StageArgumentDetails | None = None) -> str:
    """Folder with stage overlay (root overlay by default), kept in stage directory of project."""
    return os.path.join(project_directory.stage_directory_path(name=stage.name), _FILE_OVERLAY_FOLDERS.get(argument, "root_overlay"))

def default_root_overlay_sources(has_parent: bool) -> list[StagePortageConfdirSource]:
    return default_portage_confdir_sources(has_parent=has_parent)

def expand_stage_path(project_directory, stage, path: str) -> str:
    """Path with placeholders of project folders replaced (@STAGE_DIR@, @PROJECT_DIR@, @REPO_DIR@)."""
    releng_directory = project_directory.get_releng_directory()
    for placeholder, value in (("@STAGE_DIR@", project_directory.stage_directory_path(name=stage.name)),
                               ("@PROJECT_DIR@", project_directory.directory_path()),
                               ("@REPO_DIR@", releng_directory.directory_path() if releng_directory else "")):
        path = path.replace(placeholder, value)
    return os.path.normpath(path)

def root_overlay_values(project_directory, stage, argument: StageArgumentDetails | None = None) -> list:
    """Selected sources of stage overlay (root overlay by default)."""
    argument = argument or root_overlay_argument(stage)
    value = getattr(stage, argument.name, None) if argument else None
    values = value if isinstance(value, list) else [value] if value is not None else []
    result = []
    for item in values:
        if item not in result:
            result.append(item)
    return result

def root_overlay_stages(project_directory, stage, argument: StageArgumentDetails | None = None, _visited: frozenset = frozenset()) -> list:
    """Stages whose overlay folders are used by stage, in order they are applied (like overlay_stages)."""
    if stage is None or stage.id in _visited:
        return []
    argument = argument or root_overlay_argument(stage)
    values = root_overlay_values(project_directory, stage, argument)
    stages = []
    if StagePortageConfdirSource.PARENT_OVERLAY in values:
        # Parent uses root overlay argument of its own target (eg. stage4/root_overlay), ISO overlay is the same.
        parent_argument = None if argument in ROOT_OVERLAY_ARGUMENTS else argument
        stages += root_overlay_stages(project_directory, _parent_stage(project_directory, stage), parent_argument, _visited | {stage.id})
    if StagePortageConfdirSource.STAGE_OVERLAY in values:
        stages.append(stage)
    return stages

def parent_root_overlay_stages(project_directory, stage, argument: StageArgumentDetails | None = None) -> list:
    argument = argument or root_overlay_argument(stage)
    parent_argument = None if argument in ROOT_OVERLAY_ARGUMENTS else argument
    return root_overlay_stages(project_directory, _parent_stage(project_directory, stage), parent_argument, frozenset({stage.id}))

def releng_root_overlay_paths(project_directory, stage, argument: StageArgumentDetails | None = None) -> list[str]:
    """Overlay folders defined by releng template of stage."""
    from .project_stage_value_resolver import load_stage_releng_template_values
    argument = argument or root_overlay_argument(stage)
    template_values = load_stage_releng_template_values(project_directory=project_directory, stage=stage) or {}
    value = template_values.get(argument.value) if argument else None
    paths = value if isinstance(value, list) else [value] if value else []
    releng_directory = project_directory.get_releng_directory()
    repo_dir = releng_directory.directory_path() if releng_directory else ""
    return [os.path.normpath(path.replace("@REPO_DIR@", repo_dir)) for path in paths]

def root_overlay_folders(project_directory, stage, argument: StageArgumentDetails | None = None) -> list[str]:
    """Folders combined to overlay of stage (root overlay by default), in order they are applied."""
    argument = argument or root_overlay_argument(stage)
    values = root_overlay_values(project_directory, stage, argument)
    folders = []
    if StagePortageConfdirSource.RELENG in values:
        folders += releng_root_overlay_paths(project_directory, stage, argument)
    # Paths set directly (stored by older versions, or by templates).
    folders += [expand_stage_path(project_directory, stage, value) for value in values if isinstance(value, str)]
    if StagePortageConfdirSource.PARENT_OVERLAY in values:
        folders += [stage_root_overlay_path(project_directory, item, argument)
                    for item in parent_root_overlay_stages(project_directory, stage, argument)]
    if StagePortageConfdirSource.STAGE_OVERLAY in values:
        folders.append(stage_root_overlay_path(project_directory, stage, argument))
    return folders

def root_overlay_options(project_directory, stage, argument: StageArgumentDetails | None = None) -> list[StageArgumentOption]:
    argument = argument or root_overlay_argument(stage)
    kind, kinds = ("ISO overlay", "ISO overlays") if argument == StageArgumentDetails.livecd_overlay else ("Root overlay", "root overlays")
    # Releng:
    releng_paths = releng_root_overlay_paths(project_directory, stage, argument)
    releng_directory = project_directory.get_releng_directory()
    repo_dir = releng_directory.directory_path() if releng_directory else None
    releng = StageArgumentOption(
        raw=StagePortageConfdirSource.RELENG, value=StagePortageConfdirSource.RELENG, argument=argument,
        display="Releng template",
        subtitle=", ".join(os.path.relpath(path, repo_dir) if repo_dir and path.startswith(repo_dir) else path for path in releng_paths)
            if releng_paths else f"{kind} defined by releng template (current template has none)"
    )
    # Parent overlay:
    from .project_stage import parent_supports_argument
    has_parent = _parent_stage(project_directory, stage) is not None
    parent_supported = parent_supports_argument(project_directory, stage, argument)
    parent_stages = parent_root_overlay_stages(project_directory, stage, argument)
    parent_files = sum(count_files(stage_root_overlay_path(project_directory, item, argument)) for item in parent_stages)
    parent = StageArgumentOption(
        raw=StagePortageConfdirSource.PARENT_OVERLAY, value=StagePortageConfdirSource.PARENT_OVERLAY, argument=argument,
        display="Parent overlay",
        subtitle=(
            f"{kinds[0].upper() + kinds[1:]} used by parent stage" if not has_parent else
            f"Target of parent stage doesn't have {kind.lower() if kind == 'Root overlay' else kind}" if not parent_supported else
            f"{_files_text(parent_files)} from {', '.join(item.name for item in parent_stages)}" if parent_stages else
            f"Parent stages don't use {kinds}"
        ),
        unsupported=not parent_supported
    )
    # Stage overlay:
    stage_files = count_files(stage_root_overlay_path(project_directory, stage, argument))
    own = StageArgumentOption(
        raw=StagePortageConfdirSource.STAGE_OVERLAY, value=StagePortageConfdirSource.STAGE_OVERLAY, argument=argument,
        display="Stage overlay",
        subtitle=f"{_files_text(stage_files)} in {_FILE_OVERLAY_FOLDERS.get(argument, 'root_overlay')} folder of stage, overwrite releng and parent files"
    )
    return [releng, parent, own]

# ------------------------------------------------------------------------------
# Files of stage overlays that stage already inherits from parent overlays.

def _same_file(path: str, other: str) -> bool:
    if os.path.islink(path) or os.path.islink(other):
        return os.path.islink(path) and os.path.islink(other) and os.readlink(path) == os.readlink(other)
    return os.path.isfile(other) and filecmp.cmp(path, other, shallow=False)

def remove_inherited_files(folder: str, inherited_folders: list[str]) -> list[str]:
    """Removes files of folder identical to files inherited from folders combined before it (later folders overwrite
    files of earlier ones), so folder keeps only what it adds or changes. Folders left empty are removed. Returns
    removed files, relative to folder."""
    removed = []
    if not inherited_folders or not os.path.isdir(folder):
        return removed
    for root, directories, files in os.walk(folder, topdown=False):
        for name in files + [directory for directory in directories if os.path.islink(os.path.join(root, directory))]:
            path = os.path.join(root, name)
            relative = os.path.relpath(path, folder)
            # File used from inherited folders is the one of last folder that has it.
            inherited = next((os.path.join(item, relative) for item in reversed(inherited_folders)
                              if os.path.lexists(os.path.join(item, relative))), None)
            if inherited is not None and _same_file(path, inherited):
                os.remove(path)
                removed.append(relative)
        if root != folder and not os.path.islink(root) and not os.listdir(root):
            os.rmdir(root)
    if not os.listdir(folder):
        os.rmdir(folder)
    return sorted(removed)

def remove_stage_overlays_inherited_files(project_directory, stage) -> dict[str, list[str]]:
    """Removes files of stage overlay folders (Portage configuration, root and ISO overlays) that stage already gets
    from parent overlays. Only folders of overlays using parent overlay are changed. Returns removed files by folder."""
    removed = {}
    folders = []
    if StagePortageConfdirSource.PARENT_OVERLAY in portage_confdir_sources(stage):
        folders.append((stage_overlay_path(project_directory, stage),
                        [stage_overlay_path(project_directory, item) for item in parent_overlay_stages(project_directory, stage)]))
    for argument in FILE_OVERLAY_ARGUMENTS:
        if argument in ROOT_OVERLAY_ARGUMENTS and argument != root_overlay_argument(stage):
            continue
        if argument == StageArgumentDetails.livecd_overlay and (getattr(stage, "target", None) or "").replace("-", "_") != "livecd_stage2":
            continue
        if StagePortageConfdirSource.PARENT_OVERLAY in root_overlay_values(project_directory, stage, argument):
            folders.append((stage_root_overlay_path(project_directory, stage, argument),
                            [stage_root_overlay_path(project_directory, item, argument)
                             for item in parent_root_overlay_stages(project_directory, stage, argument)]))
    for folder, inherited in folders:
        if files := remove_inherited_files(folder, inherited):
            removed[os.path.basename(folder)] = files
    return removed
