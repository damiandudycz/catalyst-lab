from __future__ import annotations
import os, subprocess, ast, uuid, tomllib
from functools import lru_cache
from .repository import Serializable
from .toolset import Toolset, ToolsetApplication, ToolsetEnv
from .root_helper_client import RootHelperClient
from .releng_directory import RelengDirectory
from .event_bus import EventBus, SharedEvent
from .architecture import Architecture
from .snapshot import PortageProfile
from .snapshot import Snapshot
from .repository import Repository
from typing import Any
from dataclasses import dataclass
from typing import Self, FrozenSet
from enum import Enum, auto
from collections.abc import Iterable
from .project_stage_arguments import (
    StageArguments, StageArgumentTargetDetails, StageArgumentOption,
    StageArgumentType, StageArgumentDetails
)
from .project_stage_compression_mode import StageCompressionMode
from .project_stage_argument_serialization import ProjectStageArgumentSerialization
from .project_stage_automatic_option import StageAutomaticOption
from .project_stage_portage_confdir import (
    portage_confdir_options, default_portage_confdir_sources, root_overlay_options, default_root_overlay_sources, FILE_OVERLAY_ARGUMENTS,
    ROOT_OVERLAY_ARGUMENTS, root_overlay_argument
)

class ProjectStage(Serializable):

    def __init__(self, id: uuid.UUID | None = None, parent_id: uuid.UUID | None = None, name: str = None, target_name: str | None = None, releng_template_name: str | None = None, profile: PortageProfile | None = None, data: dict = {}):
        self.event_bus = EventBus[ProjectStageEvent]()
        # Required variables in case not set in json:
        self.id = id or uuid.uuid4()
        self.parent = parent_id
        self.target = target_name
        self.name = name
        self.releng_template = releng_template_name
        self.profile = profile
        # Load available values from data dictionary
        for key, value in data.items():
            setattr(self, key, ProjectStageArgumentSerialization.deserialize(value))

    # Quick access properties:

    # --------------------------------------------------------------------------

    def serialize(self) -> dict:
        attrs = self.__dict__.copy()
        attrs.pop('event_bus', None)
        new_attrs = attrs.copy()
        # Map types to serialized format
        for key, value in attrs.items():
            if value is None:
                new_attrs.pop(key, None)
            else:
                new_attrs[key] = ProjectStageArgumentSerialization.serialize(value)
        return new_attrs

    @classmethod
    def init_from(cls, data: dict) -> Self:
        return cls(data=data)

# Icons of stages by kind of catalyst target (stage1-4, livecd_stage1-2, diskimage_stage1-2, netboot, embedded).
_STAGE_TARGET_ICONS = {
    "stage": "stage-archive-symbolic",
    "livecd": "stage-disc-symbolic",
    "diskimage": "stage-disk-image-symbolic",
    "netboot": "stage-netboot-symbolic",
    "embedded": "stage-embedded-symbolic",
}

def stage_target_icon(target: str | None) -> str:
    """Icon of stage with given catalyst target."""
    kind = (target or "").replace("-", "_").split("_")[0].rstrip("0123456789")
    return _STAGE_TARGET_ICONS.get(kind, "layers-minimalistic-svgrepo-com-symbolic")

# Abbreviations of words in catalyst target names, other words use their first three letters.
_STAGE_TARGET_WORD_ABBREVIATIONS = {
    "stage": "S",
    "livecd": "LCD",
    "diskimage": "DI",
    "netboot": "NB",
    "embedded": "EMB",
}

def stage_target_short_name(target: str | None) -> str:
    """Short name of catalyst target, eg. S1 for stage1, LCD2 for livecd_stage2, DI1 for diskimage_stage1. Built from
    words of target name, so targets read from catalyst that are not known here get short names too."""
    parts = [part for part in (target or "").replace("-", "_").split("_") if part]
    short_name = ""
    for index, part in enumerate(parts):
        word = part.rstrip("0123456789")
        number = part[len(word):]
        # "stage" after other word only adds its number (livecd_stage2 -> LCD2).
        abbreviation = "" if word == "stage" and index > 0 else _STAGE_TARGET_WORD_ABBREVIATIONS.get(word, word[:3].upper())
        short_name += abbreviation + number
    return short_name or "?"

class ProjectStageEvent(Enum):
    NAME_CHANGED = auto()

# ------------------------------------------------------------------------------
# Catalyst stage arguments analysis:

# Catalyst adds boot/kernel and its per-kernel arguments dynamically, so the
# static argument scanner cannot discover kernel support from target source.
TARGETS_SUPPORTING_KERNEL_BOOT_OPTIONS = frozenset({
    "livecd_stage2",
    "stage4",
})

def load_catalyst_stage_arguments(toolset: Toolset, target_name: str | None) -> StageArguments:
    """Loads the list of arguments used by catalyst targets"""

    def extract_frozenset_values(code_str: str) -> dict[str, list[str]]:
        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.values = {
                    "required_values": [],
                    "valid_values": [],
                }
                # Keep track of values assigned so far for substitution
                self._cache = {
                    "required_values": [],
                    "valid_values": [],
                }
            def visit_ClassDef(self, node: ast.ClassDef):
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, ast.Name):
                                attr_name = target.id
                                if attr_name in self.values:
                                    # Extract new elements from right side
                                    new_elements = self._extract_strings_from_expr(stmt.value)
                                    # Update cache and values
                                    self._cache[attr_name].extend(new_elements)
                                    self.values[attr_name] = list(set(self._cache[attr_name]))  # unique
                    elif isinstance(stmt, ast.AugAssign):
                        target = stmt.target
                        if isinstance(target, ast.Name):
                            attr_name = target.id
                            if attr_name in self.values:
                                new_elements = self._extract_strings_from_expr(stmt.value)
                                self._cache[attr_name].extend(new_elements)
                                self.values[attr_name] = list(set(self._cache[attr_name]))
                self.generic_visit(node)
            def visit_Assign(self, node):
                self._handle_assignment(node.targets[0], node.value)
            def visit_AugAssign(self, node):
                self._handle_assignment(node.target, node.value)
            def _handle_assignment(self, target, value):
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                    attr_name = target.attr
                    if attr_name in self.values:
                        new_elements = self._extract_strings_from_expr(value)
                        self.values[attr_name].extend(new_elements)
            def _extract_strings_from_expr(self, expr):
                elements = []
                if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "frozenset":
                    if expr.args and isinstance(expr.args[0], (ast.List, ast.Set, ast.Tuple)):
                        for elt in expr.args[0].elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                elements.append(elt.value)
                elif isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
                    elements.extend(self._extract_strings_from_expr(expr.left))
                    elements.extend(self._extract_strings_from_expr(expr.right))
                elif isinstance(expr, ast.Name):
                    # Substitute from cached values
                    name = expr.id
                    if name in self._cache:
                        elements.extend(self._cache[name])
                return elements
        tree = ast.parse(code_str)
        visitor = Visitor()
        visitor.visit(tree)
        return visitor.values

    if toolset.get_app_install(ToolsetApplication.CATALYST) is None:
        raise RuntimeError("This toolset does not have Catalyst installed.")
    if toolset.env != ToolsetEnv.EXTERNAL:
        raise RuntimeError("Currently only EXTERNAL toolsets are supported for this functionality.")

    toolset_file_path = toolset.file_path()
    catalyst_path = "/usr/lib/python*/site-packages/catalyst"
    catalyst_path_base = os.path.join(catalyst_path, "base")
    catalyst_path_targets = os.path.join(catalyst_path, "targets")
    if target_name is not None:
        catalyst_path_stage = os.path.join(catalyst_path_targets, f"{target_name}.py")
    else:
        catalyst_path_stage = os.path.join(catalyst_path_base, "stagebase.py")

    # Find stage .py path:
    output = subprocess.check_output(
        ['unsquashfs', '-l', toolset_file_path, catalyst_path_stage],
        text=True
    )
    catalyst_path_stage_found = None
    catalyst_path_stage_basename = os.path.basename(catalyst_path_stage)
    for line in output.splitlines():
        line = line.strip()
        if line.endswith(catalyst_path_stage_basename):
            catalyst_path_stage_found = line[len('squashfs-root/'):]
            break
    if not catalyst_path_stage_found:
        raise FileNotFoundError("Could not find stage .py in the squashfs archive")

    # Read stage arguments:
    stage_content = subprocess.check_output(
        ['unsquashfs', '-cat', toolset_file_path, catalyst_path_stage_found],
        text=True
    )
    results = extract_frozenset_values(stage_content)
    required_values = results["required_values"]
    valid_values = list(set(results["required_values"]) | set(results["valid_values"]))

    # Append virtual values:
    valid_values.append(StageArgumentDetails.name.value)
    required_values.append(StageArgumentDetails.name.value)
    valid_values.append(StageArgumentDetails.parent.value)
    if target_name and target_name != 'stage1':
        required_values.append(StageArgumentDetails.parent.value)
    valid_values.append(StageArgumentDetails.releng_template.value)

    # Check if needs to also load stagebase and combine
    if target_name is not None:
        # Load the base stage arguments (stagebase.py)
        base_args = load_catalyst_stage_arguments(toolset, target_name=None)
        # Merge required and valid
        required = frozenset(required_values)
        valid = frozenset(valid_values)
        # Combine with base
        combined_required = required | base_args.required
        combined_valid = valid | base_args.valid
        return StageArguments(
            required=combined_required,
            valid=combined_valid
        )
    else:
        return StageArguments(
            required=frozenset(required_values),
            valid=frozenset(valid_values)
        )

# Arguments of targets by toolset file and target, reading them unpacks files of toolset. Toolsets are updated by
# writing new file in place of old one, so key includes modification time and size of file: updated toolset is read
# again. Toolsets of different projects are different files.
_target_arguments_cache: dict[tuple, frozenset[str]] = {}

def target_argument_names(toolset: Toolset | None, target_name: str) -> frozenset[str] | None:
    """Arguments valid for target in catalyst of toolset, None when they can't be read."""
    try:
        path = toolset.file_path()
        status = os.stat(path)
        key = (path, status.st_mtime_ns, status.st_size, target_name)
    except Exception:
        return None
    if key not in _target_arguments_cache:
        try:
            _target_arguments_cache[key] = frozenset(load_catalyst_stage_arguments(toolset=toolset, target_name=target_name).valid)
        except Exception as e:
            print(f"Failed to read arguments of {target_name}: {e}")
            return None
    return _target_arguments_cache[key]

def parent_supports_argument(project_directory, stage, argument: StageArgumentDetails | None) -> bool:
    """Stage has parent whose target has the argument too, so its value can be inherited. Root overlay of parent is
    root overlay argument of its target (eg. stage4/root_overlay for livecd/root_overlay)."""
    parent_id = getattr(stage, StageArgumentDetails.parent.name, None)
    parent = next((item for item in project_directory.stages if item.id == parent_id), None) if parent_id else None
    if parent is None or argument is None:
        return False
    if argument in ROOT_OVERLAY_ARGUMENTS:
        argument = root_overlay_argument(parent)
        if argument is None:
            return False
    names = target_argument_names(project_directory.get_toolset(), parent.target)
    return True if names is None else argument.value in names # Assumed supported when arguments can't be read.

def remove_unsupported_parent_options(project_directory, stage) -> list[str]:
    """Removes inheriting from parent (inherit option, parent overlay source) from arguments whose value parent can't
    give (its target doesn't have the argument). Arguments left empty are cleared. Returns changed arguments."""
    from .project_stage_portage_confdir import StagePortageConfdirSource
    parent_values = (StageAutomaticOption.INHERIT_FROM_PARENT, StagePortageConfdirSource.PARENT_OVERLAY)
    changed = []
    for argument in StageArgumentDetails:
        value = getattr(stage, argument.name, None)
        values = value if isinstance(value, list) else [value]
        if not any(item in parent_values for item in values if isinstance(item, (StageAutomaticOption, StagePortageConfdirSource))):
            continue
        if parent_supports_argument(project_directory, stage, argument):
            continue
        kept = [item for item in values if not (isinstance(item, (StageAutomaticOption, StagePortageConfdirSource)) and item in parent_values)]
        setattr(stage, argument.name, (kept if isinstance(value, list) else kept[0]) if kept else None)
        changed.append(argument.value)
    return changed

def load_catalyst_stage_arguments_details(toolset: Toolset, target_name: str | None) -> dict[str, StageArgumentTargetDetails]:
    arguments_sets = load_catalyst_stage_arguments(toolset=toolset, target_name=target_name)
    # Catalyst adds boot/kernel dynamically from the spec's values in
    # StageBase.set_valid_build_kernel_vars(), so static source inspection can't
    # discover it for targets that support kernels. Keep it available for targets
    # in TARGETS_SUPPORTING_KERNEL_BOOT_OPTIONS when the AST extractor misses it.
    # Stage4's unmerge is also declared dynamically in some Catalyst layouts.
    fallback_arguments = set()
    normalized_target = target_name.replace("-", "_") if target_name else ""
    if normalized_target in TARGETS_SUPPORTING_KERNEL_BOOT_OPTIONS:
        fallback_arguments.add(StageArgumentDetails.boot_kernel.value)
    if normalized_target.startswith("stage4"):
        fallback_arguments.add(StageArgumentDetails.stage4_unmerge.value)
    missing_arguments = fallback_arguments - arguments_sets.valid
    if missing_arguments:
        arguments_sets = StageArguments(
            required=arguments_sets.required,
            valid=arguments_sets.valid | missing_arguments,
        )
    arguments_details = {
        arg_name: StageArgumentTargetDetails(
            name=arg_name,
            required=(arg_name in arguments_sets.required),
            details=StageArgumentDetails.named(name=arg_name)
        )
        for arg_name in arguments_sets.valid
    }
    return dict(sorted(arguments_details.items()))

def load_catalyst_stage_arguments_options(project_directory, stage: ProjectStage, arg_details: StageArgumentTargetDetails | None) -> list[StageArgumentOption] | None:
    """Load possible list of options to select if argument allows it."""
    if not arg_details:
        return None
    match arg_details.details:
        case StageArgumentDetails.target:
            values = load_catalyst_targets(toolset=project_directory.get_toolset())
            return [StageArgumentOption(raw=value, display=value, subtitle=None, value=value, argument=arg_details.details) for value in values]
        case StageArgumentDetails.portage_confdir:
            return portage_confdir_options(project_directory=project_directory, stage=stage)
        case StageArgumentDetails.stage4_root_overlay | StageArgumentDetails.livecd_root_overlay | StageArgumentDetails.livecd_overlay:
            return root_overlay_options(project_directory=project_directory, stage=stage, argument=arg_details.details)
        case StageArgumentDetails.profile:
            # Project getting latest snapshot has no snapshot before its first build, only profiles of overlays are known.
            snapshot = project_directory.get_snapshot()
            values = snapshot.load_profiles(arch=project_directory.get_architecture()) if snapshot else []
            values += load_stage_overlay_profiles(project_directory=project_directory, stage=stage)
            return [StageArgumentOption(raw=value.path, display=value.path, subtitle=value.repo, value=value, argument=arg_details.details) for value in values]
        case StageArgumentDetails.releng_template:
            values = load_releng_templates(releng_directory=project_directory.get_releng_directory(), stage_name=stage.target, architecture=project_directory.get_architecture())
            return [StageArgumentOption(raw=value, display=value, subtitle=None, value=value, argument=arg_details.details) for value in values]
        case StageArgumentDetails.snapshot_treeish:
            values = Repository.Snapshot.value
            return [StageArgumentOption(raw=value, display=value.name, subtitle=value.short_details, value=value, argument=arg_details.details) for value in values]
        case StageArgumentDetails.compression_mode:
            values = StageCompressionMode
            return [StageArgumentOption(raw=value, display=value.name, subtitle=None, value=value, argument=arg_details.details) for value in values]
        case StageArgumentDetails.repos:
            values = Repository.OverlayDirectory.value
            return [StageArgumentOption(raw=value, display=value.name, subtitle=None, value=value.id, argument=arg_details.details) for value in values]
        case StageArgumentDetails.interpreter:
            values = project_directory.get_toolset().metadata.get(ToolsetApplication.QEMU.package, {}).get('interpreters', [])
            return [StageArgumentOption(raw=value, display=value, subtitle=None, value=value, argument=arg_details.details) for value in values]
        case StageArgumentDetails.parent:
            values = load_stage_possible_seeds(stage=stage, project_directory=project_directory)
            return [StageArgumentOption(raw=value, display=value.name, subtitle=None, value=value.id, argument=arg_details.details, icon_name=stage_target_icon(value.target)) for value in values]
        case StageArgumentDetails.subarch:
            values = load_catalyst_subarches(toolset=project_directory.get_toolset(), architecture=project_directory.get_architecture())
            return [StageArgumentOption(raw=value.name, display=value.name, subtitle=value.chost or value.common_flags, value=value.name, argument=arg_details.details) for value in values]
    return None

def load_stage_overlay_profiles(project_directory, stage: ProjectStage) -> list[PortageProfile]:
    """Profiles defined by overlays used by stage (repos argument, following inheritance)."""
    from .project_stage_value_resolver import resolve_stage_argument
    overlay_ids = resolve_stage_argument(project_directory, stage, StageArgumentDetails.repos.value)
    if not isinstance(overlay_ids, list):
        return []
    profiles = []
    for overlay in Repository.OverlayDirectory.value:
        if overlay.id in overlay_ids:
            profiles += overlay.load_profiles(arch=project_directory.get_architecture())
    return profiles

def load_catalyst_stage_arguments_options_for_boolean(arg_details: StageArgumentTargetDetails | None) -> list[StageArgumentOption] | None:
    """Creates Yes/No StageArgumentOptions for given argument."""
    return [
        StageArgumentOption(raw=True, display="Yes", subtitle=None, value=True, argument=arg_details.details),
        StageArgumentOption(raw=False, display="No", subtitle=None, value=False, argument=arg_details.details)
    ]

def load_catalyst_stage_automatic_arguments_options(stage: ProjectStage, arg_details: StageArgumentTargetDetails | None,
                                                    project_directory=None) -> list[StageArgumentOption] | None:
    """Creates additional inheriting options for given argument. With project, inheriting from parent is unavailable
    when target of parent doesn't have the argument."""
    if not arg_details:
        return []
    opt_parent = StageArgumentOption(raw=StageAutomaticOption.INHERIT_FROM_PARENT, display="Inherit from parent", subtitle="Set to the value used by parent stage", value=StageAutomaticOption.INHERIT_FROM_PARENT, argument=arg_details.details)
    opt_releng = StageArgumentOption(raw=StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE, display="Inherit from Releng template", subtitle="Set to the value used by releng template", value=StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE, argument=arg_details.details)
    opt_auto   = StageArgumentOption(raw=StageAutomaticOption.GENERATE_AUTOMATICALLY, display="Automatic", subtitle="Automatically set to correct value", value=StageAutomaticOption.GENERATE_AUTOMATICALLY, argument=arg_details.details)
    if getattr(stage, StageArgumentDetails.parent.name, None) is None:
        opt_parent.unsupported = True
    elif project_directory is not None and arg_details.details and not parent_supports_argument(project_directory, stage, arg_details.details):
        opt_parent.unsupported = True
        opt_parent.subtitle = "Target of parent stage doesn't have this setting"
    if getattr(stage, StageArgumentDetails.releng_template.name, None) is None:
        opt_releng.unsupported = True
    if not arg_details.details:
        return []
    options = {
        StageAutomaticOption.INHERIT_FROM_PARENT: opt_parent,
        StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE: opt_releng,
        StageAutomaticOption.GENERATE_AUTOMATICALLY: opt_auto,
    }
    return [options[automatic_option] for automatic_option in arg_details.details.automatic_options]

def apply_default_stage_arguments(project_directory, stage: ProjectStage) -> dict[str, Any]:
    """Sets default automatic options (inherit from parent, releng template...) for arguments valid for stage target.
    From default options of argument available for the stage (parent selected, releng template selected...), first one
    that resolves to some value is used. If none of them does, first available is used anyway, as releng template or
    parent can define the value later. Returns arguments that were set."""
    from .project_stage_value_resolver import resolve_stage_argument, UNRESOLVED
    has_parent = getattr(stage, StageArgumentDetails.parent.name, None) is not None
    has_template = bool(getattr(stage, StageArgumentDetails.releng_template.name, None))
    def is_available(argument: StageArgumentDetails, option: StageAutomaticOption) -> bool:
        match option:
            case StageAutomaticOption.INHERIT_FROM_PARENT:
                return has_parent and parent_supports_argument(project_directory, stage, argument)
            case StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE:
                return has_template # Even if template doesn't define it now, templates can define any argument.
            case StageAutomaticOption.GENERATE_AUTOMATICALLY:
                return True
        return False
    def has_value(argument: StageArgumentDetails, option: StageAutomaticOption) -> bool:
        if option == StageAutomaticOption.GENERATE_AUTOMATICALLY:
            return True # Determined when building, even if not known yet.
        value = resolve_stage_argument(project_directory, stage, argument.value, option=option)
        return value is not UNRESOLVED and value not in (None, "", [])
    arguments = load_catalyst_stage_arguments_details(toolset=project_directory.get_toolset(), target_name=stage.target)
    applied = {}
    for name, argument in arguments.items():
        if argument.details is None or getattr(stage, argument.attribute_name, None) is not None:
            continue
        if argument.details == StageArgumentDetails.portage_confdir:
            setattr(stage, argument.attribute_name, default_portage_confdir_sources(has_parent=has_parent))
            applied[name] = getattr(stage, argument.attribute_name)
            continue
        if argument.details in FILE_OVERLAY_ARGUMENTS:
            setattr(stage, argument.attribute_name, default_root_overlay_sources(
                has_parent=has_parent and parent_supports_argument(project_directory, stage, argument.details)))
            applied[name] = getattr(stage, argument.attribute_name)
            continue
        if argument.details == StageArgumentDetails.boot_kernel:
            # Releng kernel inheritance is the default only when the selected spec
            # actually supplies kernels. Do not persist an empty inherit marker.
            option = StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE
            if has_template and has_value(argument.details, option):
                setattr(stage, argument.attribute_name, option)
                applied[name] = option
            continue
        available = [option for option in argument.details.default_options if is_available(argument.details, option)]
        option = next((option for option in available if has_value(argument.details, option)), available[0] if available else None)
        if option is not None:
            setattr(stage, argument.attribute_name, option)
            applied[name] = option
    return applied

def load_catalyst_targets(toolset: Toolset) -> list[str]:
    """Loads the list of available targets as their paths inside squashfs file"""

    if toolset.get_app_install(ToolsetApplication.CATALYST) is None:
        raise RuntimeError("This toolset does not have Catalyst installed.")
    if toolset.env != ToolsetEnv.EXTERNAL:
        raise RuntimeError("Currently only EXTERNAL toolsets are supported for this functionality.")

    toolset_file_path = toolset.file_path()
    catalyst_path = "/usr/lib/python*/site-packages/catalyst"
    catalyst_path_targets = os.path.join(catalyst_path, "targets")

    # Read the list of potential target files:
    output = subprocess.check_output(
        ['unsquashfs', '-l', toolset_file_path, f"{catalyst_path_targets}/*.py"],
        text=True
    )
    except_files = {'__init__.py', 'snapshot.py'}
    target_files = [
        os.path.splitext(os.path.basename(line))[0]
        for line in output.splitlines()
        if line.strip().endswith('.py') and os.path.basename(line) not in except_files
    ]
    return target_files

@dataclass(frozen=True)
class CatalystSubarch:
    """Subarchitecture definition from catalyst arch/*.toml files."""
    name: str
    chost: str | None
    common_flags: str | None

def load_catalyst_subarches(toolset: Toolset | None, architecture: Architecture | None) -> list[CatalystSubarch]:
    """Loads subarches available for given architecture from catalyst arch/*.toml files in toolset."""
    if toolset is None or architecture is None:
        return []
    if toolset.get_app_install(ToolsetApplication.CATALYST) is None:
        raise RuntimeError("This toolset does not have Catalyst installed.")
    if toolset.env != ToolsetEnv.EXTERNAL:
        raise RuntimeError("Currently only EXTERNAL toolsets are supported for this functionality.")
    toolset_file_path = toolset.file_path()
    arch_tables = _load_catalyst_arch_tables(toolset_file_path, os.path.getmtime(toolset_file_path))
    subarches = []
    for table in architecture.catalyst_arch_tables():
        for name, values in arch_tables.get(table.name, {}).items():
            chost = values.get("CHOST")
            if table.chost_prefix and chost and not chost.startswith(table.chost_prefix):
                continue
            subarches.append(CatalystSubarch(name=name, chost=chost, common_flags=values.get("COMMON_FLAGS")))
    return subarches

@lru_cache(maxsize=8)
def _load_catalyst_arch_tables(toolset_file_path: str, toolset_mtime: float) -> dict[str, dict[str, dict]]:
    """Reads and merges all catalyst arch/*.toml files from toolset squashfs. Cached per toolset file version."""
    catalyst_arch_path = "/usr/share/catalyst/arch"
    output = subprocess.check_output(
        ['unsquashfs', '-l', toolset_file_path, f"{catalyst_arch_path}/*.toml"],
        text=True
    )
    tables: dict[str, dict[str, dict]] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.endswith('.toml') or not line.startswith('squashfs-root/'):
            continue
        toml_path = line[len('squashfs-root/'):]
        try:
            content = subprocess.check_output(['unsquashfs', '-cat', toolset_file_path, toml_path], text=True)
            for table_name, subarches in tomllib.loads(content).items():
                if table_name == "setarch" or not isinstance(subarches, dict):
                    continue
                tables.setdefault(table_name, {}).update(subarches)
        except (subprocess.CalledProcessError, tomllib.TOMLDecodeError) as e:
            print(f"Warning: Failed to read {toml_path}: {e}")
    return tables

# ------------------------------------------------------------------------------
# Loading releng templates:

def load_releng_templates(releng_directory: RelengDirectory, stage_name: str, architecture: Architecture) -> list[str]:
    specs_path = os.path.join(releng_directory.directory_path(), "releases/specs")
    templates = []
    base_arch = architecture.releng_base_arch() if architecture else None
    if base_arch is None or not os.path.isdir(specs_path):
        # Some Catalyst architectures (including MIPS) have no matching Releng
        # specs. Releng is optional in the stage wizard, so keep target selection
        # usable and show no templates for these architectures.
        return templates
    prefix = base_arch.value + "/"
    for root, _, files in os.walk(specs_path):
        for file in files:
            if file.endswith(".spec"):
                full_path = os.path.join(root, file)
                # Check if file contains the desired stage_name
                try:
                    with open(full_path, 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("target:") and line.split(":", 1)[1].strip() == stage_name.replace('_', '-'):
                                rel_path = os.path.relpath(full_path, specs_path)
                                if rel_path.startswith(prefix):
                                    rel_path = rel_path.removeprefix(prefix)
                                    templates.append(rel_path)
                                    break
                except Exception as e:
                    print(f"Warning: Failed to read {full_path}: {e}")
    return templates

def load_stage_possible_seeds(stage: ProjectStage, project_directory: ProjectDirectory):
    def _get_descendant_ids(stage: ProjectStage, project_directory: ProjectDirectory) -> list[int]:
        project_stages_tree = project_directory.stages_tree()
        def find_node(nodes: list, target_id: int):
            for node in nodes:
                if node.value.id == target_id:
                    return node
                result = find_node(node.children, target_id)
                if result:
                    return result
            return None
        def collect_ids(node) -> list[int]:
            ids = []
            for child in node.children:
                ids.append(child.value.id)
                ids.extend(collect_ids(child))
            return ids
        root_node = find_node(project_stages_tree, stage.id)
        if not root_node:
            return []
        return collect_ids(root_node)
    child_ids = _get_descendant_ids(stage=stage, project_directory=project_directory)
    available_stages = project_directory.stages[:]
    values = [
        s for s in available_stages
        if s.id not in child_ids and s.id != stage.id
    ]
    selected = next(
        (stage for stage in available_stages if stage.id == stage.parent),
        None
    )
    return values

