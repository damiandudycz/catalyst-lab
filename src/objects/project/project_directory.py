from __future__ import annotations
from typing import Self, final
from dataclasses import dataclass
from .git_directory import GitDirectory, GitDirectoryEvent
from .repository import Serializable, Repository, resolve_path
from .project_stage import ProjectStage
from .tree_node import TreeNode
from .architecture import Architecture
import uuid, json, os
import re


@dataclass
class BrokenProjectStage:
    """A stage entry that must remain visible but cannot be used for builds or editing."""
    id: uuid.UUID
    name: str
    warning_message: str
    parent: uuid.UUID | None = None
    target: str | None = None
    is_broken: bool = True

@final
class ProjectDirectory(GitDirectory):

    @classmethod
    def base_location(cls) -> str:
        return resolve_path(Repository.Settings.value.project_location)

    @classmethod
    def parse_metadata(cls, dict: dict) -> Serializable:
        return ProjectConfiguration.init_from(data=dict)

    @property
    def stages(self) -> list[ProjectStage]:
        if not hasattr(self, '_stages'):
            self._stages = []
            self._broken_stages = []
            stages_dir = os.path.join(self.directory_path(), "stages")
            try:
                with os.scandir(stages_dir) as entries:
                    items = sorted(
                        entry.name for entry in entries
                        if not entry.name.startswith(".") and entry.is_dir()
                    )
            except FileNotFoundError:
                items = []
            for item in items:
                stage_path = os.path.join(stages_dir, item)
                config_path = os.path.join(stage_path, "stage.json")
                raw = ""
                try:
                    with open(config_path, 'r', encoding='utf-8') as f:
                        raw = f.read()
                    stage = ProjectStage.init_from(data=json.loads(raw))
                    self._stages.append(stage)
                except Exception as e:
                    # Keep malformed stages visible and preserve their current-format identity when possible.
                    def property_value(key: str) -> str | None:
                        match = re.search(rf'"{re.escape(key)}"\s*:\s*\{{[^}}]*?"value"\s*:\s*"([^"]+)"',
                                          raw, re.DOTALL)
                        return match.group(1) if match else None

                    recovered_id = property_value("id")
                    try:
                        stage_id = uuid.UUID(recovered_id) if recovered_id else None
                    except ValueError:
                        stage_id = None
                    stage_id = stage_id or uuid.uuid5(uuid.NAMESPACE_URL, f"{self.directory_path()}/stages/{item}")
                    recovered_parent = property_value("parent")
                    try:
                        parent_id = uuid.UUID(recovered_parent) if recovered_parent else None
                    except ValueError:
                        parent_id = None
                    stage_name = property_value("name") or item
                    self._broken_stages.append(BrokenProjectStage(
                        id=stage_id,
                        name=stage_name,
                        warning_message=f"Could not load stage: {e}",
                        parent=parent_id,
                    ))
                    print(f"Failed to load stage from {config_path}: {e}")
        return self._stages
    @stages.setter
    def stages(self, value: list[ProjectStage]):
        self._stages = value

    def add_stage(self, stage: ProjectStage):
        self.stages.append(stage)
        self.event_bus.emit(GitDirectoryEvent.CONTENT_CHANGED, self)

    def stages_tree(self) -> list[dict]:
        """Builds a tree of stages for seeds inheritance."""
        # Broken stage files remain visible in the project tree, but are not part of
        # `stages`, so the build and stage-editing code only receives usable stages.
        visible_stages = [*self.stages, *getattr(self, "_broken_stages", [])]
        stage_nodes = {stage.id: TreeNode(value=stage) for stage in visible_stages}
        roots = []
        missing_parents = {}
        for stage_id, node in stage_nodes.items():
            parent_id = node.value.parent
            if parent_id and parent_id in stage_nodes:
                stage_nodes[parent_id].children.append(node)
            elif parent_id:
                # Keep the broken relationship visible in the tree. A synthetic warning
                # node is intentionally not a ProjectStage and cannot be selected/edited.
                parent_node = missing_parents.get(parent_id)
                if parent_node is None:
                    missing = BrokenProjectStage(
                        id=uuid.uuid5(uuid.NAMESPACE_OID, str(parent_id)),
                        name="?",
                        warning_message=f"Parent stage {parent_id} was not found",
                    )
                    parent_node = missing_parents[parent_id] = TreeNode(value=missing)
                    roots.append(parent_node)
                parent_node.children.append(node)
            else:
                roots.append(node)
        return roots

    @property
    def builds_summary(self) -> str:
        """Short description of project stage builds, eg. for builds list."""
        from .project_build import load_project_builds
        from .project_build_process import running_project_build
        builds = load_project_builds(self)
        summary = f"{len(builds)} build{'s' if len(builds) != 1 else ''}, last {builds[0].date.strftime('%Y-%m-%d %H:%M')}" if builds else "No builds yet"
        return f"Building now · {summary}" if running_project_build(self) else summary

    @property
    def configuration_error(self) -> str | None:
        """Why project can't be built because of its configuration (toolset, releng directory, snapshot)."""
        if self.get_toolset() is None:
            return "Toolset is not selected or was removed"
        if self.get_releng_directory() is None:
            return "Releng directory is not selected or was removed"
        if self.get_snapshot() is None and not self.uses_latest_snapshot:
            return "Snapshot is not selected or was removed"
        return None

    def _last_build_run(self):
        """Result of newest build run, whether project is being built, and timestamp of newest run."""
        from .project_build import last_build_run
        from .project_build_process import running_project_build
        running = running_project_build(self)
        timestamp, result = last_build_run(self, running_timestamp=running.timestamp if running else None)
        if running is not None and timestamp == running.timestamp:
            # Steps of running build that failed (also steps not building stages, eg. seed download) fail the run
            # already, records of stages depending on them are updated when build finishes.
            from .multistage_process import MultiStageProcessStageState
            from .project_build import BuildRunResult
            if any(step.state == MultiStageProcessStageState.FAILED for step in running.stages):
                result = BuildRunResult.FAILED
        return result, running is not None, timestamp

    def mark_build_result_seen(self):
        """Result of newest build run was seen (project or its builds were opened), it's not marked in projects and
        builds lists anymore."""
        _, _, timestamp = self._last_build_run()
        if timestamp is None or self.metadata is None or self.metadata.seen_build_timestamp == timestamp:
            return
        self.metadata.seen_build_timestamp = timestamp
        Repository.ProjectDirectory.save()
        from .event_bus import SharedEvent
        self.event_bus.emit(SharedEvent.STATE_UPDATED, self)

    @property
    def build_status_indicator_values(self):
        """Builds list: last build run completed (succeeded), failed (error) or stopped (warning), until builds or
        project were opened. Blinking while building."""
        from .project_build import BuildRunResult
        from .status import StatusIndicatorState, StatusDetail, item_status
        result, building, timestamp = self._last_build_run()
        match result:
            case BuildRunResult.FAILED: state, description = StatusIndicatorState.ERROR, "Last build failed"
            case BuildRunResult.STOPPED: state, description = StatusIndicatorState.WARNING, "Last build was stopped"
            case BuildRunResult.COMPLETED: state, description = StatusIndicatorState.SUCCEEDED, "Last build completed"
            case _: state, description = StatusIndicatorState.IDLE, "No builds"
        if self.metadata is not None and self.metadata.seen_build_timestamp == timestamp:
            state = StatusIndicatorState.IDLE # Result was seen, it's still described.
        return item_status([StatusDetail(state, description),
                            StatusDetail(StatusIndicatorState.LOADED, "Building") if building else None], blinking=building)

    @property
    def status_indicator_values(self):
        """Projects list: configuration error, failed or completed last build (until project is opened), Git status
        (changes, errors), blinking while building."""
        from .project_build import BuildRunResult
        from .status import StatusIndicatorState, StatusDetail, item_status
        result, building, timestamp = self._last_build_run()
        details = []
        if configuration_error := self.configuration_error:
            details.append(StatusDetail(StatusIndicatorState.ERROR, configuration_error))
        unseen = self.metadata is None or self.metadata.seen_build_timestamp != timestamp
        if result == BuildRunResult.FAILED and unseen:
            details.append(StatusDetail(StatusIndicatorState.ERROR, "Last build failed"))
        elif result == BuildRunResult.COMPLETED and unseen and not building:
            details.append(StatusDetail(StatusIndicatorState.SUCCEEDED, "Last build completed"))
        details += self.status_details()
        if building:
            details.append(StatusDetail(StatusIndicatorState.LOADED, "Building"))
        return item_status(details, blinking=building or self.is_busy)

    @property
    def deploy_summary(self) -> str:
        """Builds of project that can be deployed, for Deploy section."""
        from .project_build import load_project_builds
        from .deploy_installation import is_deployable
        from .deploy_live_iso import is_live_iso_build
        builds = [build for build in load_project_builds(self) if is_deployable(self, build) or is_live_iso_build(build)]
        if not builds:
            return "No stage3, stage4 or live CD builds"
        return f"{len(builds)} build{'s' if len(builds) != 1 else ''} ready to deploy, last {builds[0].date.strftime('%Y-%m-%d %H:%M')}"

    @property
    def deploy_status_indicator_values(self):
        """Deploy section: blinking while build of project is being deployed, result of finished deployment until it's
        seen."""
        from .deploy_installation import DeployInstallation
        from .deploy_live_iso import LiveIsoDeployment
        from .multistage_process import MultiStageProcess
        from .status import item_status, process_result_status
        processes = [process for process in dict.fromkeys(MultiStageProcess.started_processes + MultiStageProcess.finished_processes)
                     if isinstance(process, (DeployInstallation, LiveIsoDeployment)) and getattr(process, "project_id", None) == self.id]
        values = [value for process in processes if (value := process_result_status(process)) is not None]
        return item_status([detail for value in values for detail in value.details],
                           blinking=any(value.blinking for value in values))

    def initialize_metadata(self) -> ProjectConfiguration:
        if not self.metadata:
            self.metadata = ProjectConfiguration()
        return self.metadata

    def _get_by_id(self, items, target_id, attr):
        if not target_id:
            return None
        return next((item for item in items if getattr(item, attr) == target_id), None)

    def get_toolset(self) -> Toolset | None:
        if self.metadata is None:
            return None
        return self._get_by_id(Repository.Toolset.value, self.metadata.toolset_id, 'uuid')

    def get_releng_directory(self) -> RelengDirectory | None:
        if self.metadata is None:
            return None
        return self._get_by_id(Repository.RelengDirectory.value, self.metadata.releng_directory_id, 'id')

    def get_snapshot(self) -> Snapshot | None:
        """Snapshot of project, newest snapshot when project uses latest snapshot."""
        if self.metadata is None:
            return None
        if self.metadata.latest_snapshot:
            from .snapshot_manager import sorted_snapshots
            return next(iter(sorted_snapshots(Repository.Snapshot.value)), None)
        return self._get_by_id(Repository.Snapshot.value, self.metadata.snapshot_id, 'filename')

    @property
    def uses_latest_snapshot(self) -> bool:
        """Builds generate latest snapshot by default."""
        return self.metadata is not None and self.metadata.latest_snapshot

    def get_packages_directory(self):
        """Binary packages folder of project, None when not set (or removed)."""
        from .packages_directory import packages_directory_for_id
        if self.metadata is None:
            return None
        return packages_directory_for_id(self.metadata.packages_directory_id)

    def get_architecture(self) -> Architecture | None:
        if self.metadata is None:
            return None
        return self.metadata.architecture

    # TODO: Maybe move this and some other methods to project manager?
    @classmethod
    def stage_directory_path_for_name(cls, name: str, project: ProjectDirectory) -> str:
        project_path = project.directory_path()
        return os.path.join(
            project_path, "stages",
            cls.sanitized_name_for_name(name)
        )

    def stage_directory_path(self, name: str) -> str:
        return ProjectDirectory.stage_directory_path_for_name(name=name, project=self)

    def install_stage(self, stage: ProjectStage):
        # Save stage details in project directory
        from .project_manager import ProjectManager
        if not ProjectManager.shared().is_stage_name_available(project=self, name=stage.name):
            raise RuntimeError(f"Stage with name {stage.name} already exists in this project.")
        ProjectManager.shared().save_stage(project=self, stage=stage)

@dataclass
class ProjectConfiguration(Serializable):
    toolset_id: uuid.UUID | None = None
    releng_directory_id: uuid.UUID | None = None
    snapshot_id: str | None = None
    latest_snapshot: bool = False # Builds generate latest snapshot, newest snapshot is used as snapshot of project.
    architecture: Architecture | None = None
    seen_build_timestamp: str | None = None # Newest build run whose result was seen (project was opened).
    packages_directory_id: uuid.UUID | None = None # Binary packages folder (shared by projects).

    def serialize(self) -> dict:
        return {
            "toolset_id": str(self.toolset_id) if self.toolset_id else None,
            "releng_directory_id": str(self.releng_directory_id) if self.releng_directory_id else None,
            "snapshot_id": self.snapshot_id,
            "latest_snapshot": self.latest_snapshot,
            "architecture": self.architecture.value if self.architecture else None,
            "seen_build_timestamp": self.seen_build_timestamp,
            "packages_directory_id": str(self.packages_directory_id) if self.packages_directory_id else None,
        }

    @classmethod
    def init_from(cls, data: dict) -> Self:
        try:
            toolset_id = uuid.UUID(data["toolset_id"]) if data.get("toolset_id") else None
            releng_directory_id = uuid.UUID(data["releng_directory_id"]) if data.get("releng_directory_id") else None
            snapshot_id = data["snapshot_id"] if data.get("snapshot_id") else None
            architecture = Architecture(data["architecture"]) if data.get("architecture") else None
        except KeyError:
            raise ValueError(f"Failed to parse {data}")
        return cls(
            toolset_id=toolset_id,
            releng_directory_id=releng_directory_id,
            snapshot_id=snapshot_id,
            latest_snapshot=bool(data.get("latest_snapshot", False)),
            architecture=architecture,
            seen_build_timestamp=data.get("seen_build_timestamp"),
            packages_directory_id=uuid.UUID(data["packages_directory_id"]) if data.get("packages_directory_id") else None,
        )

