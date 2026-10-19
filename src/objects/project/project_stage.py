from __future__ import annotations
import os, subprocess, ast, uuid, tomllib
from functools import lru_cache
from .repository import Serializable
from .toolset import Toolset, ToolsetApplication, ToolsetEnv
from .releng_directory import RelengDirectory
from .event_bus import EventBus
from .architecture import Architecture
from .snapshot import PortageProfile
from .repository import Repository
from typing import Any
from dataclasses import dataclass
from typing import Self
from enum import Enum, auto
from .project_stage_arguments import (
    StageArguments, StageArgumentTargetDetails, StageArgumentOption,
    StageArgumentDetails, parent_stage
)
from .project_stage_compression_mode import StageCompressionMode
from .project_stage_argument_serialization import ProjectStageArgumentSerialization
from .project_stage_automatic_option import StageAutomaticOption
from .project_stage_portage_confdir import (
    portage_confdir_options, default_portage_confdir_sources, root_overlay_options, FILE_OVERLAY_ARGUMENTS,
    ROOT_OVERLAY_ARGUMENTS, root_overlay_argument
)
from .tree_node import walk_tree

_CATALYSTLAB_METADATA_KEY = "_catalystlab"
# These describe the stage itself and must never be populated or removed as
# calculated Catalyst defaults.
_STAGE_IDENTITY_ARGUMENTS = frozenset({"id", "parent", "target", "name", "releng_template"})

class ProjectStage(Serializable):

    def __init__(self, id: uuid.UUID | None = None, parent_id: uuid.UUID | None = None, name: str = None, target_name: str | None = None, releng_template_name: str | None = None, profile: PortageProfile | None = None, data: dict | None = None):
        self.event_bus = EventBus[ProjectStageEvent]()
        self._explicit_none_arguments = set()
        self._default_stage_arguments = {}
        self._explicit_stage_arguments = set()
        self._explicit_user_arguments = set()
        self._explicit_user_argument_dates = {}
        # Required variables in case not set in json:
        self.id = id or uuid.uuid4()
        self.parent = parent_id
        self.target = target_name
        self.name = name
        self.releng_template = releng_template_name
        self.profile = profile
        # Load available values from data dictionary
        for key, value in (data or {}).items():
            if key == _CATALYSTLAB_METADATA_KEY:
                if isinstance(value, dict):
                    # Backward compatibility with stage files written before
                    # timestamps lived alongside their property values.
                    self._explicit_user_argument_dates = dict(value.get("last_changed", {}))
                    # Read the in-development marker format if a project was saved before timestamps were added.
                    legacy_explicit = set(value.get("explicit", []))
                    self._explicit_user_arguments = legacy_explicit
                    for name in legacy_explicit:
                        self._explicit_user_argument_dates.setdefault(name, "legacy")
                continue
            if key == "toolset_id":
                continue
            if isinstance(value, dict) and "last_changed" in value:
                changed_at = value.get("last_changed")
                if changed_at is not None:
                    self._explicit_user_argument_dates[key] = changed_at
                value = {field: item for field, item in value.items() if field != "last_changed"}
            value = ProjectStageArgumentSerialization.deserialize(value)
            setattr(self, key, value)
            self._explicit_stage_arguments.add(key)
            if value is None:
                self._explicit_none_arguments.add(key)

    # Quick access properties:

    # --------------------------------------------------------------------------

    def serialize(self) -> dict:
        result = {
            key: ProjectStageArgumentSerialization.serialize(value)
            for key, value in self.__dict__.items()
            if key not in {"event_bus", "_explicit_none_arguments", "_default_stage_arguments",
                           "_explicit_stage_arguments", "_explicit_user_arguments",
                           "_explicit_user_argument_dates"}
            and (key not in self._default_stage_arguments or key in self._explicit_stage_arguments)
            and (value is not None or key in self._explicit_none_arguments)
        }
        for name, changed_at in self._explicit_user_argument_dates.items():
            if changed_at is not None and name in result and isinstance(result[name], dict):
                result[name]["last_changed"] = changed_at
        return result

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

def parent_supports_argument(project_directory, stage, argument: StageArgumentDetails | None, toolset=None) -> bool:
    """Whether a value can be inherited from a parent stage with the same target argument."""
    # A root overlay is applied while building the parent image, so descendants
    # already contain those files. Reapplying it as a separate source is invalid.
    if argument in ROOT_OVERLAY_ARGUMENTS:
        return False
    parent = parent_stage(project_directory, stage)
    if parent is None or argument is None:
        return False
    if argument in ROOT_OVERLAY_ARGUMENTS:
        argument = root_overlay_argument(parent)
        if argument is None:
            return False
    if toolset is None:
        from .toolset_selection import default_toolset
        toolset = default_toolset()
    names = target_argument_names(toolset, parent.target) if toolset else None
    return True if names is None else argument.value in names # Assumed supported when arguments can't be read.

def remove_unsupported_parent_options(project_directory, stage, toolset=None) -> list[str]:
    """Removes parent inheritance from unsupported arguments, including all root-overlay inheritance. Returns changed arguments."""
    from .project_stage_portage_confdir import StagePortageConfdirSource
    parent_values = (StageAutomaticOption.INHERIT_FROM_PARENT, StagePortageConfdirSource.PARENT_OVERLAY)
    changed = []
    for argument in StageArgumentDetails:
        value = getattr(stage, argument.name, None)
        values = value if isinstance(value, list) else [value]
        if not any(item in parent_values for item in values if isinstance(item, (StageAutomaticOption, StagePortageConfdirSource))):
            continue
        if parent_supports_argument(project_directory, stage, argument, toolset):
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

def load_catalyst_stage_arguments_options(project_directory, stage: ProjectStage, arg_details: StageArgumentTargetDetails | None,
                                          toolset=None) -> list[StageArgumentOption] | None:
    """Load possible list of options to select if argument allows it."""
    if not arg_details:
        return None
    match arg_details.details:
        case StageArgumentDetails.target:
            if toolset is None:
                from .toolset_selection import default_toolset
                toolset = default_toolset()
            values = load_catalyst_targets(toolset=toolset) if toolset else []
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
            from .toolset_selection import default_toolset
            qemu_toolset = default_toolset([ToolsetApplication.QEMU])
            values = qemu_toolset.metadata.get(ToolsetApplication.QEMU.package, {}).get('interpreters', []) if qemu_toolset else []
            return [StageArgumentOption(raw=value, display=value, subtitle=None, value=value, argument=arg_details.details) for value in values]
        case StageArgumentDetails.parent:
            values = load_stage_possible_seeds(stage=stage, project_directory=project_directory)
            return [StageArgumentOption(raw=value, display=value.name, subtitle=None, value=value.id, argument=arg_details.details, icon_name=stage_target_icon(value.target)) for value in values]
        case StageArgumentDetails.subarch:
            if toolset is None:
                from .toolset_selection import default_toolset
                toolset = default_toolset()
            values = load_catalyst_subarches(toolset=toolset, architecture=project_directory.get_architecture()) if toolset else []
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

def _preferred_default_option(argument: StageArgumentDetails, *options: StageAutomaticOption):
    """Create a project-aware preference function for one default-table entry."""
    def choose(stage: ProjectStage, project_directory, toolset, snapshot=None) -> StageAutomaticOption | None:
        has_parent = getattr(stage, StageArgumentDetails.parent.name, None) is not None
        has_template = bool(getattr(stage, StageArgumentDetails.releng_template.name, None))
        for option in options:
            if option not in argument.automatic_options:
                continue
            if option == StageAutomaticOption.INHERIT_FROM_PARENT:
                if not has_parent or not parent_supports_argument(project_directory, stage, argument, toolset):
                    continue
            elif option == StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE and not has_template:
                continue
            elif option == StageAutomaticOption.GENERATE_AUTOMATICALLY:
                # Automatic is itself a deliberate default (not a computed preview value).
                return option
            # Inheritance is valid based on its source being available, even if
            # that source currently leaves this particular argument unset.
            return option
        return None
    return choose


def _preferred_option_or_value(argument: StageArgumentDetails, *options: StageAutomaticOption, fallback: Any):
    """Prefer an available inheritance option, then use a concrete usable fallback."""
    choose = _preferred_default_option(argument, *options)

    def choose_or_fallback(stage: ProjectStage, project_directory, toolset, snapshot=None):
        return choose(stage, project_directory, toolset, snapshot=snapshot) or fallback

    return choose_or_fallback


def _default_subarch(stage: ProjectStage, project_directory, toolset, snapshot=None):
    """Use the template or parent subarch when defined, otherwise choose a generic arch baseline."""
    from .project_stage_value_resolver import resolve_stage_argument, UNRESOLVED

    if getattr(stage, StageArgumentDetails.releng_template.name, None):
        value = resolve_stage_argument(
            project_directory, stage, StageArgumentDetails.subarch.value,
            option=StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE,
        )
        if value is not UNRESOLVED and value not in (None, "", []):
            return StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE
    if (getattr(stage, StageArgumentDetails.parent.name, None) is not None
            and parent_supports_argument(project_directory, stage, StageArgumentDetails.subarch, toolset)):
        value = resolve_stage_argument(
            project_directory, stage, StageArgumentDetails.subarch.value,
            option=StageAutomaticOption.INHERIT_FROM_PARENT,
        )
        if value is not UNRESOLVED and value not in (None, "", []):
            return StageAutomaticOption.INHERIT_FROM_PARENT

    architecture = project_directory.get_architecture()
    preferred_names = _DEFAULT_SUBARCH_BY_ARCHITECTURE.get(architecture, ())
    try:
        available_names = {item.name for item in load_catalyst_subarches(toolset, architecture)}
    except Exception as error:
        print(f"Failed to choose a default subarch for {architecture}: {error}")
        return None
    return next((name for name in preferred_names if name in available_names), None)


def _default_profile(stage: ProjectStage, project_directory, toolset, snapshot=None):
    """Inherit a real profile when possible, otherwise choose the snapshot's generic stable profile."""
    from .project_stage_value_resolver import resolve_stage_argument, UNRESOLVED

    if getattr(stage, StageArgumentDetails.releng_template.name, None):
        value = resolve_stage_argument(
            project_directory, stage, StageArgumentDetails.profile.value,
            option=StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE,
        )
        if value is not UNRESOLVED and value not in (None, "", []):
            return StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE
    if (getattr(stage, StageArgumentDetails.parent.name, None) is not None
            and parent_supports_argument(project_directory, stage, StageArgumentDetails.profile, toolset)):
        value = resolve_stage_argument(
            project_directory, stage, StageArgumentDetails.profile.value,
            option=StageAutomaticOption.INHERIT_FROM_PARENT,
        )
        if value is not UNRESOLVED and value not in (None, "", []):
            return StageAutomaticOption.INHERIT_FROM_PARENT

    snapshot = snapshot or project_directory.get_snapshot()
    architecture = project_directory.get_architecture()
    if snapshot is None or architecture is None:
        return None
    try:
        profiles = snapshot.load_profiles(arch=architecture)
    except Exception as error:
        print(f"Failed to choose a default profile for {architecture}: {error}")
        return None

    prefix = f"default/linux/{architecture.value}/"
    candidates = [profile for profile in profiles
                  if profile.repo == "gentoo" and profile.stability == "stable" and profile.path.startswith(prefix)]
    if not candidates:
        return None

    def profile_rank(profile):
        parts = profile.path[len(prefix):].split("/")
        # A direct child of the architecture directory is Gentoo's generic profile;
        # deeper profiles enable desktop, init-system, or other specialized choices.
        generic = len(parts) == 1
        version = tuple(int(part) for part in parts[0].split(".") if part.isdigit())
        return generic, -len(parts), version

    return max(candidates, key=profile_rank)


_RELENG = StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE
_PARENT = StageAutomaticOption.INHERIT_FROM_PARENT
_AUTOMATIC = StageAutomaticOption.GENERATE_AUTOMATICALLY
_DEFAULT_SUBARCH_BY_ARCHITECTURE = {
    # Prefer generic entries without CPU-specific flags where Catalyst provides them.
    Architecture.x86: ("i686", "i486", "x86"),
    Architecture.amd64: ("amd64", "x86_64"),
    Architecture.arm: ("arm", "armv7a_hardfp", "armv7a"),
    Architecture.arm64: ("arm64", "aarch64"),
    Architecture.hppa: ("hppa1.1", "hppa2.0"),
    Architecture.ia64: ("ia64",),
    Architecture.mips: ("mips1", "mips32", "mipsel1", "mips64"),
    Architecture.ppc: ("ppc", "powerpc"),
    Architecture.ppc64: ("ppc64", "powerpc64", "power5"),
    Architecture.ppc64le: ("ppc64le", "powerpc64le", "power7le"),
    Architecture.riscv: ("riscv", "rv64_lp64d"),
    Architecture.sparc: ("sparc64", "sparc"),
    Architecture.alpha: ("alpha", "ev4"),
    Architecture.m68k: ("m68k",),
    Architecture.loong: ("loong",),
    Architecture.s390: ("s390",),
    Architecture.s390x: ("s390x",),
}
_RELENG_TARGET_DEFAULTS = {
    argument.name: _preferred_default_option(argument, _RELENG)
    for argument in StageArgumentDetails
    if argument.value.split("/")[0] in ("stage4", "livecd", "boot")
    and argument not in (StageArgumentDetails.stage4_packages, StageArgumentDetails.stage4_root_overlay,
                         StageArgumentDetails.livecd_root_overlay,
                         StageArgumentDetails.livecd_overlay)
}
_RELENG_CAPABLE_DEFAULTS = {
    argument.name: _preferred_default_option(
        argument,
        _RELENG,
        *(option for option in argument.automatic_options if option != _RELENG),
    )
    for argument in StageArgumentDetails
    if _RELENG in argument.automatic_options
    # Interpreter is intentionally Automatic by default; template inheritance is an explicit user choice.
    and argument != StageArgumentDetails.interpreter
}

# Default values for known Catalyst target types. A target-specific entry takes
# precedence over ANY; a missing value falls through to ANY and then to None.
# Callable values receive (stage, project_directory, toolset) and can pick a
# supported inheritance option or compute a concrete value from project state.
STAGE_DEFAULT_VALUES: dict[str, dict[str, Any]] = {
    "ANY": {
        **_RELENG_TARGET_DEFAULTS,
        # Every known property that supports template inheritance gets it as the
        # first choice. Explicit entries below can still define special fallbacks.
        **_RELENG_CAPABLE_DEFAULTS,
        # Build compatibility and architecture:
        "profile": _default_profile,
        "subarch": _default_subarch,
        "chost": _preferred_default_option(StageArgumentDetails.chost, _RELENG, _PARENT),
        "cbuild": _preferred_default_option(StageArgumentDetails.cbuild, _RELENG, _PARENT),
        "interpreter": _AUTOMATIC,
        "asflags": _preferred_default_option(StageArgumentDetails.asflags, _RELENG, _PARENT),
        "cflags": _preferred_default_option(StageArgumentDetails.cflags, _RELENG, _PARENT),
        "common_flags": _preferred_default_option(StageArgumentDetails.common_flags, _RELENG, _PARENT),
        "cxxflags": _preferred_default_option(StageArgumentDetails.cxxflags, _RELENG, _PARENT),
        "fcflags": _preferred_default_option(StageArgumentDetails.fcflags, _RELENG, _PARENT),
        "fflags": _preferred_default_option(StageArgumentDetails.fflags, _RELENG, _PARENT),
        "ldflags": _preferred_default_option(StageArgumentDetails.ldflags, _RELENG, _PARENT),
        # Release and package sources:
        "rel_type": _preferred_option_or_value(
            StageArgumentDetails.rel_type, _RELENG, _PARENT, fallback="default"),
        # Build-spec generation replaces @TIMESTAMP@ with the shared timestamp for that build run.
        "version_stamp": _preferred_option_or_value(
            StageArgumentDetails.version_stamp, _RELENG, _PARENT, fallback="@TIMESTAMP@"),
        "compression_mode": _preferred_default_option(StageArgumentDetails.compression_mode, _RELENG),
        "stage4_packages": _preferred_default_option(StageArgumentDetails.stage4_packages, _RELENG, _PARENT),
        "repos": _preferred_default_option(StageArgumentDetails.repos, _RELENG, _PARENT),
        "keep_repos": _preferred_default_option(StageArgumentDetails.keep_repos, _RELENG, _PARENT),
        "binrepo_path": _preferred_default_option(StageArgumentDetails.binrepo_path, _RELENG, _PARENT),
        # Configuration:
        "portage_prefix": _preferred_default_option(StageArgumentDetails.portage_prefix, _RELENG, _PARENT),
        "update_seed": _preferred_default_option(StageArgumentDetails.update_seed, _RELENG, _PARENT),
        "update_seed_command": _preferred_default_option(StageArgumentDetails.update_seed_command, _RELENG, _PARENT),
        "rename_regexp": _preferred_default_option(StageArgumentDetails.rename_regexp, _RELENG),
        "install_mask": _preferred_default_option(StageArgumentDetails.install_mask, _RELENG, _PARENT),
        "catalyst_use": _preferred_default_option(StageArgumentDetails.catalyst_use, _RELENG, _PARENT),
        "decompressor_search_order": _preferred_default_option(
            StageArgumentDetails.decompressor_search_order, _RELENG, _PARENT),
        "hostuse": _preferred_default_option(StageArgumentDetails.hostuse, _RELENG),
        "boot_kernel": _preferred_default_option(StageArgumentDetails.boot_kernel, _RELENG),
        # These are generated by CatalystLab when a build needs them, not persisted as stage defaults.
        "source_subpath": None,
        "pkgcache_path": None,
        "kerncache_path": None,
    },
    # Each known target prefers its Releng-specified toolchain and release identity.
    "stage1": {
        "subarch": _default_subarch,
        "rel_type": _RELENG,
    },
    "stage2": {"subarch": _default_subarch, "rel_type": _RELENG},
    "stage3": {"subarch": _default_subarch, "rel_type": _RELENG},
    "stage4": {
        "subarch": _default_subarch,
        "rel_type": _RELENG,
        "stage4_packages": _preferred_default_option(StageArgumentDetails.stage4_packages, _RELENG, _PARENT),
    },
    "livecd_stage1": {"subarch": _default_subarch, "rel_type": _RELENG},
    "livecd_stage2": {"subarch": _default_subarch, "rel_type": _RELENG},
    "diskimage_stage1": {"subarch": _default_subarch, "rel_type": _RELENG},
    "diskimage_stage2": {"subarch": _default_subarch, "rel_type": _RELENG},
    "netboot": {"subarch": _default_subarch, "rel_type": _RELENG},
    "embedded": {"subarch": _default_subarch, "rel_type": _RELENG},
}


_NO_DEFAULT = object()


def default_stage_argument_value(project_directory, stage: ProjectStage, argument: StageArgumentDetails, toolset,
                                 snapshot=None):
    """Get a target-specific default, then the shared default; unresolved settings remain unset."""
    target = (getattr(stage, StageArgumentDetails.target.name, None) or "").replace("-", "_")
    target_defaults = STAGE_DEFAULT_VALUES.get(target, {})

    def resolve_candidate(candidate):
        if callable(candidate):
            candidate = candidate(stage, project_directory, toolset, snapshot=snapshot)
            if candidate is None:
                return _NO_DEFAULT
        elif candidate is None:
            return None # Explicit null means leave the value unset and don't use ANY.
        if isinstance(candidate, StageAutomaticOption):
            if candidate not in argument.automatic_options:
                return _NO_DEFAULT
            if candidate == StageAutomaticOption.INHERIT_FROM_PARENT:
                if (getattr(stage, StageArgumentDetails.parent.name, None) is None
                        or not parent_supports_argument(project_directory, stage, argument, toolset)):
                    return _NO_DEFAULT
            elif candidate == StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE:
                if not getattr(stage, StageArgumentDetails.releng_template.name, None):
                    return _NO_DEFAULT
        return candidate

    if argument.name in target_defaults:
        value = resolve_candidate(target_defaults[argument.name])
        if value is not _NO_DEFAULT:
            return value
    value = resolve_candidate(STAGE_DEFAULT_VALUES["ANY"].get(argument.name))
    return None if value is _NO_DEFAULT else value


def refresh_default_stage_arguments(project_directory, stage: ProjectStage, toolset=None, snapshot=None) -> dict[str, Any]:
    """Recompute in-memory defaults without making them explicit project edits."""
    has_parent = getattr(stage, StageArgumentDetails.parent.name, None) is not None
    defaults = getattr(stage, "_default_stage_arguments", {})
    explicit = getattr(stage, "_explicit_stage_arguments", set())
    explicit_none = getattr(stage, "_explicit_none_arguments", set())
    for name in _STAGE_IDENTITY_ARGUMENTS:
        defaults.pop(name, None)
    for name in list(defaults):
        if name in explicit:
            continue
        if hasattr(stage, name):
            delattr(stage, name)
        explicit_none.discard(name)
    defaults.clear()

    if toolset is None:
        from .toolset_selection import default_toolset
        toolset = default_toolset()
    if toolset is None:
        return {}
    arguments = load_catalyst_stage_arguments_details(toolset=toolset, target_name=stage.target)
    applied = {}
    calculated = {}
    for name, argument in arguments.items():
        if argument.details is None:
            continue
        if argument.attribute_name in _STAGE_IDENTITY_ARGUMENTS:
            continue
        if argument.details == StageArgumentDetails.portage_confdir:
            value = default_portage_confdir_sources(has_parent=has_parent)
        elif argument.details in FILE_OVERLAY_ARGUMENTS:
            value = default_portage_confdir_sources(
                has_parent=has_parent and parent_supports_argument(project_directory, stage, argument.details, toolset))
        else:
            value = default_stage_argument_value(project_directory, stage, argument.details, toolset, snapshot=snapshot)
        calculated[argument.attribute_name] = value
        if (getattr(stage, argument.attribute_name, None) is not None
                or argument.attribute_name in getattr(stage, "_explicit_none_arguments", set())):
            continue
        if value is not None:
            setattr(stage, argument.attribute_name, value)
            applied[argument.attribute_name] = value
    defaults.update(calculated)
    return applied


def stage_argument_is_customized(stage: ProjectStage, name: str, explicit_arguments: set[str],
                                 template_baseline: dict) -> bool:
    """Compare an argument to its active default before falling back to its creation baseline."""
    missing = object()
    current = stage.serialize().get(name, missing)
    changed_at = getattr(stage, "_explicit_user_argument_dates", {}).get(name, missing)
    baseline_property = template_baseline.get(name, missing)
    baseline_changed_at = (baseline_property.get("last_changed", missing)
                          if isinstance(baseline_property, dict) else missing)
    if baseline_changed_at is missing:
        legacy_dates = template_baseline.get(_CATALYSTLAB_METADATA_KEY, {}).get("last_changed", {})
        baseline_changed_at = legacy_dates.get(name, missing)
    if isinstance(current, dict):
        current = {field: item for field, item in current.items() if field != "last_changed"}
    if isinstance(baseline_property, dict) and "value" in baseline_property:
        baseline_property = {field: item for field, item in baseline_property.items() if field != "last_changed"}
    if changed_at is not missing:
        if baseline_changed_at is missing or changed_at != baseline_changed_at:
            return True
        # A matching template timestamp means the value still comes from the
        # template. Runtime defaults can resolve that value differently in
        # memory (and serialize() intentionally omits calculated defaults), so
        # comparing the transient value here would mark untouched fields as
        # customized until the app is restarted.
        return False

    template_value = baseline_property
    baseline_kind = template_baseline.get("__catalystlab_baseline_kind__")
    if name in explicit_arguments:
        # Values chosen by a creation wizard are part of its saved initial state.
        # Older code may also mark those fields explicit without a user-edit date.
        if baseline_kind == "initial_snapshot" and template_value is not missing and current == template_value:
            return False
        return True

    if baseline_kind in {"first_commit", "initial_snapshot"} and template_value is not missing:
        return current != template_value

    if template_value is not missing and name != StageArgumentDetails.portage_confdir.name:
        effective_value = getattr(stage, name, missing)
        if effective_value is not missing:
            normalized_current = ProjectStageArgumentSerialization.serialize(effective_value)
            normalized_template = template_value
            if isinstance(template_value, dict) and {"type", "value"} <= template_value.keys():
                normalized_template = ProjectStageArgumentSerialization.serialize(
                    ProjectStageArgumentSerialization.deserialize(template_value)
                )
            if normalized_current == normalized_template:
                return False
        elif current == template_value:
            return False
        # A stored value that differs from its source is a real project edit;
        # calculated values that aren't stored still fall through to the
        # active runtime default below.
        if name in getattr(stage, "_explicit_stage_arguments", set()):
            return True

    # Portage confdir is partly derived from generated project files: template
    # overlays add STAGE_OVERLAY after the generic runtime default has been
    # calculated. Compare it to the saved source state first, otherwise that
    # intentional template value always differs from the runtime default
    # ([RELENG], optionally with PARENT_OVERLAY).
    if name == StageArgumentDetails.portage_confdir.name and template_value is not missing:
        effective_value = getattr(stage, name, missing)
        if effective_value is missing:
            return current != template_value
        normalized_baseline = template_value
        if isinstance(template_value, dict) and {"type", "value"} <= template_value.keys():
            normalized_baseline = ProjectStageArgumentSerialization.deserialize(template_value)
        return (ProjectStageArgumentSerialization.serialize(effective_value)
                != ProjectStageArgumentSerialization.serialize(normalized_baseline))

    runtime_default = getattr(stage, "_default_stage_arguments", {}).get(name, missing)
    if runtime_default is not missing:
        effective_value = getattr(stage, name, missing)
        if effective_value is missing:
            return runtime_default is not None
        return (ProjectStageArgumentSerialization.serialize(effective_value)
                != ProjectStageArgumentSerialization.serialize(runtime_default))

    if template_value is not missing:
        return current != template_value

    if name in {"id", "parent", "target", "name", "releng_template"}:
        return False
    # Without a source commit for this stage, stored values are its initial state.
    # Explicit timestamps above still identify intentional same-value edits.
    return False

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
    toolset_stat = os.stat(toolset_file_path)
    arch_tables = _load_catalyst_arch_tables(toolset_file_path, toolset_stat.st_mtime_ns, toolset_stat.st_size)
    subarches = []
    for table in architecture.catalyst_arch_tables():
        for name, values in arch_tables.get(table.name, {}).items():
            chost = values.get("CHOST")
            if table.chost_prefix and chost and not chost.startswith(table.chost_prefix):
                continue
            subarches.append(CatalystSubarch(name=name, chost=chost, common_flags=values.get("COMMON_FLAGS")))
    return subarches

@lru_cache(maxsize=8)
def _load_catalyst_arch_tables(toolset_file_path: str, toolset_mtime_ns: int,
                               toolset_size: int) -> dict[str, dict[str, dict]]:
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
    root_node = next((node for node, _ in walk_tree(project_directory.stages_tree())
                      if node.value.id == stage.id), None)
    child_ids = ({node.value.id for child in root_node.children for node, _ in walk_tree([child])}
                 if root_node else set())
    return [
        s for s in project_directory.stages
        if s.id not in child_ids and s.id != stage.id
    ]

