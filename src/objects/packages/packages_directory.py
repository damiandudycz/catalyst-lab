"""Binary packages folders: binary packages built by Catalyst (pkgcache), shared by projects.

Folders are in packages location (~/CatalystLab/Packages), separate from builds of projects, so projects can reuse
packages built by other projects. Each folder records architecture and CPU flags of projects it was made for: packages
of other architecture can't be used, and packages built with other CPU flags (eg. tuned for other CPU) might not run on
machines of project. Inside folder, packages are kept in subfolders by release type of stages (rel_type), like before.
"""
from __future__ import annotations
import os, shutil, uuid, json
from datetime import datetime
from typing import Self
from .repository import Serializable, Repository, resolve_path
from .architecture import Architecture
from .event_bus import EventBus
from .json_serialization import dump_json

class PackagesDirectory(Serializable):

    @classmethod
    def load_all(cls):
        items = []
        if not os.path.isdir(cls.base_location()):
            return items
        for entry in os.scandir(cls.base_location()):
            if not entry.is_dir(follow_symlinks=False) or entry.name.startswith("."):
                continue
            path = os.path.join(entry.path, "metadata.json")
            try:
                with open(path, encoding="utf-8") as file:
                    items.append(cls.init_from(json.load(file)))
            except (OSError, ValueError) as error:
                print(f"Failed to load packages directory metadata in {entry.path}: {error}")
        return items

    @classmethod
    def save_all(cls, items):
        for item in items:
            path = os.path.join(item.directory_path(), "metadata.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as file:
                dump_json(file, item.serialize())

    def __init__(self, name: str, architecture: Architecture | None, cpu_flags: str | None = None,
                 id: uuid.UUID | None = None, created: datetime | None = None):
        self.id = id or uuid.uuid4()
        self.name = name
        self.architecture = architecture
        self.cpu_flags = cpu_flags # Common flags of projects (root stages), None for defaults of architecture.
        self.created = created or datetime.now()
        self.event_bus = EventBus()

    def serialize(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "architecture": self.architecture.value if self.architecture else None,
            "cpu_flags": self.cpu_flags,
            "created": self.created.isoformat(),
        }

    @classmethod
    def init_from(cls, data: dict) -> Self:
        return cls(
            id=uuid.UUID(data["id"]),
            name=data["name"],
            architecture=Architecture(data["architecture"]) if data.get("architecture") else None,
            cpu_flags=data.get("cpu_flags"),
            created=datetime.fromisoformat(data["created"]) if data.get("created") else None,
        )

    # Location:

    @staticmethod
    def base_location() -> str:
        return resolve_path(Repository.Settings.value.packages_location)

    @staticmethod
    def sanitized_name_for_name(name: str) -> str:
        return name.replace("/", "_").replace("\0", "_")

    def directory_path(self) -> str:
        return os.path.join(self.base_location(), self.sanitized_name_for_name(self.name))

    # Projects:

    @property
    def projects(self) -> list:
        """Projects using this folder."""
        return [project for project in Repository.ProjectDirectory.value
                if project.metadata is not None and project.metadata.packages_directory_id == self.id]

    def compatibility(self, project) -> list[StatusDetail]:
        """Check only binary compatibility: architecture and CPU flags.

        The toolset that happened to build the packages is not part of compatibility; toolsets are selected per action.
        Other architecture is an error, while differing CPU flags are a warning because packages may not run on the
        project's target machines.
        """
        from .status import StatusIndicatorState, StatusDetail
        problems = []
        architecture = project.get_architecture()
        if self.architecture and architecture and self.architecture != architecture:
            problems.append(StatusDetail(StatusIndicatorState.ERROR,
                                         f"Made for {self.architecture.name}, project is {architecture.name}"))
        flags = project_cpu_flags(project)
        if self.architecture == architecture and flags != self.cpu_flags:
            problems.append(StatusDetail(StatusIndicatorState.WARNING,
                                         f"Made for CPU flags {self.cpu_flags or '(defaults)'}, project uses {flags or '(defaults)'}"))
        for stage, stage_flags, root_flags in stages_with_other_cpu_flags(project):
            problems.append(StatusDetail(StatusIndicatorState.WARNING,
                                         f"Stage {stage.name} uses CPU flags {stage_flags or '(defaults)'}, other than its root "
                                         f"stage ({root_flags or '(defaults)'}). Its packages are kept in this folder too."))
        return problems

    def is_usable_by(self, project) -> bool:
        from .status import StatusIndicatorState
        return not any(problem.state == StatusIndicatorState.ERROR for problem in self.compatibility(project))

    # Display:

    @property
    def short_details(self) -> str:
        projects = self.projects
        parts = [self.architecture.name if self.architecture else "Any architecture"]
        if self.cpu_flags:
            parts.append(self.cpu_flags)
        parts.append(f"{len(projects)} project{'s' if len(projects) != 1 else ''}" if projects else "Not used")
        return " · ".join(parts)

    @property
    def status_indicator_values(self):
        """Blinking while project using folder is being built."""
        from .status import StatusIndicatorState, StatusDetail, item_status
        from .project_build_process import running_project_build
        building = [project.name for project in self.projects if running_project_build(project)]
        return item_status([StatusDetail(StatusIndicatorState.LOADED, f"Used by build of {', '.join(building)}")
                            if building else None], blinking=bool(building))

    def size(self) -> int:
        """Size of packages in bytes (slow for large folders)."""
        total = 0
        for directory, _, files in os.walk(self.directory_path()):
            for name in files:
                try:
                    total += os.lstat(os.path.join(directory, name)).st_size
                except OSError:
                    pass
        return total

def project_cpu_flags(project) -> str | None:
    """Common flags set in root stages of project (stages without parent), None when they use defaults."""
    from .project_stage_arguments import StageArgumentDetails
    flags = sorted({value.strip() for stage in project.stages
                    if getattr(stage, StageArgumentDetails.parent.name, None) is None
                    and isinstance(value := _effective_common_flags(project, stage), str)
                    and value.strip()})
    return " | ".join(flags) or None


def _effective_common_flags(project, stage, visited: frozenset = frozenset()):
    """Resolve common flags as the stage editor does, including defaults kept only in memory."""
    from .project_stage_arguments import StageArgumentDetails, parent_stage
    from .project_stage_automatic_option import StageAutomaticOption
    from .project_stage_value_resolver import UNRESOLVED, resolve_stage_argument

    if stage is None or stage.id in visited:
        return UNRESOLVED
    argument = StageArgumentDetails.common_flags
    value = getattr(stage, argument.value, None)
    if value is not None:
        from .project_stage_value_resolver import split_additions
        options, own = split_additions(value) or (
            ([value[0]], []) if isinstance(value, list) and len(value) == 1
            and isinstance(value[0], StageAutomaticOption) else
            ([value], []) if isinstance(value, StageAutomaticOption) else ([], [])
        )
        if options:
            inherited_values = []
            for option in options:
                if option == StageAutomaticOption.INHERIT_FROM_PARENT:
                    inherited = _effective_common_flags(project, parent_stage(project, stage), visited | {stage.id})
                else:
                    inherited = resolve_stage_argument(project, stage, argument.value, option=option)
                if inherited is UNRESOLVED:
                    return UNRESOLVED
                if inherited:
                    inherited_values.append(str(inherited))
            inherited_values.extend(str(item) for item in own if str(item).strip())
            return " ".join(inherited_values) or None
        return resolve_stage_argument(project, stage, argument.value)

    # Stage defaults prefer the Releng value, then the parent.
    # Those defaults are shown and used by the editor without being written to
    # stage.json, so compatibility checks must evaluate them too.
    visited = visited | {stage.id}
    has_available_default = False
    if getattr(stage, StageArgumentDetails.releng_template.value, None):
        has_available_default = True
        releng_value = resolve_stage_argument(
            project, stage, argument.value,
            option=StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE,
        )
        if releng_value not in (UNRESOLVED, None, "", []):
            return releng_value
    parent = parent_stage(project, stage)
    if parent is not None:
        has_available_default = True
        parent_value = _effective_common_flags(project, parent, visited)
        if parent_value not in (UNRESOLVED, None, "", []):
            return parent_value
    return None if has_available_default else UNRESOLVED


def stages_with_other_cpu_flags(project) -> list[tuple]:
    """Stages built with other common flags than root stage they are built from (stage, its flags, flags of root).
    CPU flags of project (and of its binary packages folder) are flags of root stages, packages of these stages are kept
    in the same folder. Flags that can't be determined before building are skipped."""
    from .project_stage_arguments import StageArgumentDetails
    from .project_stage_value_resolver import resolve_stage_argument, parent_stage, UNRESOLVED
    def flags_of(stage):
        value = _effective_common_flags(project, stage)
        if value is UNRESOLVED:
            return UNRESOLVED
        return (value.strip() or None) if isinstance(value, str) else None
    def root_of(stage):
        visited = set()
        while (parent := parent_stage(project_directory=project, stage=stage)) is not None and parent.id not in visited:
            visited.add(stage.id)
            stage = parent
        return stage
    result = []
    for stage in project.stages:
        root = root_of(stage)
        if root is stage:
            continue
        stage_flags, root_flags = flags_of(stage), flags_of(root)
        if UNRESOLVED not in (stage_flags, root_flags) and stage_flags != root_flags:
            result.append((stage, stage_flags, root_flags))
    return result

def packages_directory_for_id(directory_id) -> PackagesDirectory | None:
    return next((directory for directory in Repository.PackagesDirectory.value if directory.id == directory_id), None)

def is_packages_name_available(name: str) -> bool:
    name = name.strip()
    return bool(name) and not name.startswith(".") and all(
        PackagesDirectory.sanitized_name_for_name(directory.name) != PackagesDirectory.sanitized_name_for_name(name)
        for directory in Repository.PackagesDirectory.value) and not os.path.exists(
        os.path.join(PackagesDirectory.base_location(), PackagesDirectory.sanitized_name_for_name(name)))

def unique_packages_name(name: str) -> str:
    candidate, number = name, 2
    while not is_packages_name_available(candidate):
        candidate, number = f"{name} - {number}", number + 1
    return candidate

def create_packages_directory(name: str, architecture: Architecture | None, cpu_flags: str | None = None) -> PackagesDirectory:
    directory = PackagesDirectory(name=name, architecture=architecture, cpu_flags=cpu_flags)
    os.makedirs(directory.directory_path(), exist_ok=True)
    Repository.PackagesDirectory.value.append(directory)
    return directory

def create_packages_directory_for_project(project) -> PackagesDirectory:
    """New folder for project, named like project, with its architecture and CPU flags."""
    return create_packages_directory(unique_packages_name(project.name), project.get_architecture(), project_cpu_flags(project))

def delete_packages_directory(directory: PackagesDirectory):
    """Deletes folder with its packages. Projects using it don't have binary packages folder anymore."""
    path = directory.directory_path()
    # Don't hide filesystem failures: otherwise the UI removes the folder from the
    # repository while its data remains on disk, with no way to explain the failure.
    if os.path.lexists(path):
        if os.path.islink(path) or not os.path.isdir(path):
            os.remove(path)
        else:
            shutil.rmtree(path)
        if os.path.lexists(path):
            raise OSError(f"Binary package folder still exists after deletion: {path}")
    for project in directory.projects:
        project.metadata.packages_directory_id = None
    Repository.ProjectDirectory.save()
    Repository.PackagesDirectory.value.remove(directory)
