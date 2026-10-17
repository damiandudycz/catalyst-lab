from __future__ import annotations
import os, re, uuid
from functools import lru_cache
from typing import Any
from .project_stage_arguments import StageArgumentDetails, StageArgumentType
from .project_stage_automatic_option import StageAutomaticOption
from .project_stage_compression_mode import StageCompressionMode
from .project_stage_portage_confdir import StagePortageConfdirSource, PORTAGE_CONFDIR_SOURCES_ORDER
from .snapshot import PortageProfile
from .repository import Repository

class _Unresolved:
    """Value of automatic option that can't be determined before building (missing parent or template, no generator)."""
    def __repr__(self):
        return "UNRESOLVED"
UNRESOLVED = _Unresolved()

# ------------------------------------------------------------------------------
# Resolving values of automatic options (inherit from parent, releng template...).
# Values are resolved only for preview. Placeholders (@TIMESTAMP@, @REPO_DIR@...)
# are kept as they are, they are replaced when stage is built.

def resolve_stage_argument(project_directory, stage, argument_name: str, option: StageAutomaticOption | None = None, _visited: frozenset = frozenset()) -> Any | None:
    """Returns value that argument will have when stage is built, following automatic options through parent stages and
    releng template. If option is given, it's resolved instead of value stored in stage.
    Returns None when value is empty (not set, or not defined by parent / template) and UNRESOLVED when it can't be
    determined (missing parent or template, parents loop, automatic value not known before building)."""
    details = StageArgumentDetails.named(argument_name)
    value = option if option is not None else getattr(stage, details.name if details else argument_name, None)
    # Inherited values joined with own value (inherit options followed by own entries): lists are joined without
    # duplicates, texts (eg. compiler flags) are joined with spaces.
    if details is not None and details.allows_additions and (combined := split_additions(value)) is not None:
        options, own = combined
        base = []
        for inherit_option in options:
            inherited = resolve_stage_argument(project_directory, stage, argument_name, option=inherit_option, _visited=_visited)
            if inherited is UNRESOLVED:
                return UNRESOLVED
            base += inherited if isinstance(inherited, list) else ([] if inherited is None else [inherited])
        if details.type == StageArgumentType.raw_single_line:
            return " ".join(str(item) for item in base + own if str(item).strip()) or None
        return list(dict.fromkeys(base + own)) or None
    # Multiselect arguments (repos, interpreter) store selected automatic option as single item list.
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], StageAutomaticOption):
        value = value[0]
    if not isinstance(value, StageAutomaticOption):
        return value
    if stage.id in _visited:
        return UNRESOLVED # Parents loop.
    visited = _visited | {stage.id}
    match value:
        case StageAutomaticOption.INHERIT_FROM_PARENT:
            parent = parent_stage(project_directory=project_directory, stage=stage)
            return resolve_stage_argument(project_directory, parent, argument_name, _visited=visited) if parent else UNRESOLVED
        case StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE:
            template_values = load_stage_releng_template_values(project_directory=project_directory, stage=stage)
            if template_values is None:
                return UNRESOLVED
            return _convert_releng_value(details=details, value=template_values.get(argument_name))
        case StageAutomaticOption.GENERATE_AUTOMATICALLY:
            generator = _automatic_value_generators.get(details)
            value = generator(project_directory, stage, visited) if generator else None
            return UNRESOLVED if value is None else value
    return UNRESOLVED

def split_additions(value) -> tuple[list[StageAutomaticOption], list] | None:
    """Inherit options and own entries of value joining them, None when value doesn't join inherited values (plain
    value, single option, or single option stored as list by multiselect rows)."""
    if not isinstance(value, list) or len(value) < 2:
        return None
    options = [item for item in value if isinstance(item, StageAutomaticOption)]
    own = [item for item in value if not isinstance(item, StageAutomaticOption)]
    if not options:
        return None
    return list(dict.fromkeys(options)), own

def parent_stage(project_directory, stage):
    parent_id = getattr(stage, StageArgumentDetails.parent.name, None)
    if not parent_id:
        return None
    return next((item for item in project_directory.stages if item.id == parent_id), None)

def stage_argument_uses_option(project_directory, stage, argument_name: str,
                               options: set[StageAutomaticOption], visited: frozenset = frozenset()) -> bool:
    """Whether a stage argument uses one of the options, directly or through its parent stages."""
    if stage is None or stage.id in visited:
        return False
    value = getattr(stage, argument_name, None)
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if value == StageAutomaticOption.INHERIT_FROM_PARENT:
        return stage_argument_uses_option(
            project_directory, parent_stage(project_directory, stage), argument_name, options, visited | {stage.id})
    return value in options

def _generate_source_subpath(project_directory, stage, visited: frozenset) -> str | None:
    """Output path of parent stage, as catalyst names it: rel_type/target-subarch-version_stamp."""
    parent = parent_stage(project_directory=project_directory, stage=stage)
    if parent is None or not parent.target:
        return None
    values = [
        resolve_stage_argument(project_directory, parent, argument.value, _visited=visited)
        for argument in (StageArgumentDetails.rel_type, StageArgumentDetails.subarch, StageArgumentDetails.version_stamp)
    ]
    if any(not value or not isinstance(value, str) for value in values):
        return None
    rel_type, subarch, version_stamp = values
    return f"{rel_type}/{parent.target.replace('_', '-')}-{subarch}-{version_stamp}"

def _cache_generator(argument: StageArgumentDetails):
    def generate(project_directory, stage, visited: frozenset) -> str | None:
        from .project_stage_cache import stage_cache_path, display_path
        path = stage_cache_path(project_directory, stage, argument)
        return display_path(path) if path else None
    return generate

# Generators of GENERATE_AUTOMATICALLY values that can be determined before building.
_automatic_value_generators = {
    StageArgumentDetails.source_subpath: _generate_source_subpath,
    StageArgumentDetails.pkgcache_path: _cache_generator(StageArgumentDetails.pkgcache_path),
    StageArgumentDetails.kerncache_path: _cache_generator(StageArgumentDetails.kerncache_path),
}

# ------------------------------------------------------------------------------
# Releng templates:

def load_stage_releng_template_values(project_directory, stage) -> dict[str, str | list[str]] | None:
    template_name = getattr(stage, StageArgumentDetails.releng_template.name, None)
    releng_directory = project_directory.get_releng_directory()
    architecture = project_directory.get_architecture()
    if not template_name or not isinstance(template_name, str) or releng_directory is None or architecture is None:
        return None
    base_arch = architecture.releng_base_arch()
    if base_arch is None:
        return None
    spec_path = os.path.join(releng_directory.directory_path(), "releases", "specs", base_arch.value, template_name)
    if not os.path.isfile(spec_path):
        return None
    return _parse_spec_file(spec_path, os.path.getmtime(spec_path))

@lru_cache(maxsize=32)
def _parse_spec_file(path: str, mtime: float) -> dict[str, str | list[str]]:
    """Parses catalyst .spec file the way catalyst SpecParser does: 'key: values', where values are split on whitespace
    and can continue on following lines without key. Single value is stored as str, multiple as list."""
    values: dict[str, list[str]] = {}
    current_key = None
    with open(path, encoding="utf-8") as file:
        for line in file:
            line = re.sub(r"\s*#.*$", "", line.strip())
            if not line:
                continue
            if ":" in line:
                current_key, value = line.split(":", 1)
                values[current_key] = value.strip().strip('"').split()
            elif current_key is not None:
                values[current_key] += line.split()
    return {key: items[0] if len(items) == 1 else items for key, items in values.items() if items}

def _convert_releng_value(details: StageArgumentDetails | None, value: str | list[str] | None) -> Any | None:
    """Converts value read from spec file to format used by stage argument."""
    if value is None:
        return None
    argument_type = details.type if details else StageArgumentType.raw_single_line
    match argument_type:
        case StageArgumentType.string_list | StageArgumentType.multiselect | StageArgumentType.raw:
            return value if isinstance(value, list) else [value]
        case StageArgumentType.boolean:
            return str(value).lower() in ("yes", "true", "1")
        case _:
            return " ".join(value) if isinstance(value, list) else value

# ------------------------------------------------------------------------------
# Displaying values:

def format_stage_argument_value(value: Any, max_items: int = 3) -> str | None:
    """Short text representation of resolved value for UI. Returns None for missing values."""
    if value is None or value is UNRESOLVED or value == [] or value == "":
        return None
    if isinstance(value, list) and value and all(isinstance(item, StagePortageConfdirSource) for item in value):
        value = sorted(value, key=PORTAGE_CONFDIR_SOURCES_ORDER.index) # In order sources are combined.
    if isinstance(value, list):
        shown = [format_stage_argument_value(item) or "?" for item in value[:max_items]]
        hidden_count = len(value) - max_items
        return ", ".join(shown) + (f" (+{hidden_count} more)" if hidden_count > 0 else "")
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, PortageProfile):
        return value.path
    if isinstance(value, StageCompressionMode):
        return value.name
    if isinstance(value, StagePortageConfdirSource):
        return value.display_name
    if isinstance(value, uuid.UUID):
        # Repos are stored by overlay id.
        overlay = next((item for item in Repository.OverlayDirectory.value if item.id == value), None)
        return overlay.name if overlay else str(value)
    return str(value)

def resolved_stage_argument_display(project_directory, stage, argument_name: str, option: StageAutomaticOption) -> str | None:
    """Text of value automatic option resolves to, "(None)" when it resolves to empty value, or None if it can't be
    determined."""
    try:
        value = resolve_stage_argument(project_directory, stage, argument_name, option=option)
        if value is UNRESOLVED:
            return None
        return format_stage_argument_value(value) or "(None)" # Same as empty selection in option rows.
    except Exception as e:
        print(f"Failed to resolve {argument_name} ({option.name}): {e}")
        return None
