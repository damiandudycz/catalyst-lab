from __future__ import annotations
import os, json, uuid, shutil
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from .repository import Repository, resolve_path
from .project_stage_arguments import StageArgumentDetails

# ------------------------------------------------------------------------------
# Stage builds storage:
# <builds_location>/<project>/stages/<stage>/<timestamp>/build.json + build results.
# <builds_location>/<project> is also used as catalyst builds directory when building, so it contains catalyst output
# (moved to stage build directory after build). Downloaded seeds are shared in Cache; generated files live in Temporary.

class StageBuildStatus(Enum):
    SCHEDULED = "scheduled" # Part of build run, didn't start yet (or app was closed before it started).
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped" # Not built, because stage it depends on failed.
    CANCELED = "canceled" # Not built, because build run was canceled.
    STOPPED = "stopped" # Building started, but build run was canceled.

    @property
    def is_attempt(self) -> bool:
        """Stage was built (or building started)."""
        return self in (StageBuildStatus.IN_PROGRESS, StageBuildStatus.COMPLETED, StageBuildStatus.FAILED, StageBuildStatus.STOPPED)

@dataclass
class StageBuild:
    """Record of single stage build, stored in build.json inside its build directory."""
    stage_id: uuid.UUID
    stage_name: str
    timestamp: str # Value of @TIMESTAMP@ used for this build, also name of build directory.
    status: StageBuildStatus = StageBuildStatus.IN_PROGRESS
    date: datetime = field(default_factory=datetime.now)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    path: str | None = None # Build directory, set when loaded or saved.
    artifact: str | None = None # Filename of built stage archive, inside build directory.
    order: int | None = None # Position of stage in build order of its run.
    finished: datetime | None = None # When build completed or failed.
    # What was built and how, for reproducing builds and reporting bugs (see BUILD_DETAILS): inputs (spec, snapshot,
    # toolset, catalyst, project commit, seed), environment (where it ran, resources), failure (failed packages,
    # reason) and output (archive size, installed packages).
    details: dict = field(default_factory=dict)

    METADATA_FILE = "build.json"
    SPEC_FILE = "stage.spec" # Spec used by catalyst, copied to build directory.
    PORTAGE_DIRECTORY = "portage" # Portage configuration used by catalyst (portage_confdir).
    PACKAGES_FILE = "packages.txt" # Packages installed in built stage.
    FAILURE_DIRECTORY = "failure" # Logs of failed packages (build.log, environment, emerge --info...) for bug reports.

    @property
    def duration(self):
        """Time of building, None when build didn't finish."""
        return self.finished - self.date if self.finished else None

    @property
    def is_usable(self) -> bool:
        """Can be used as seed for other stages."""
        return self.status == StageBuildStatus.COMPLETED

    def serialize(self) -> dict:
        return {
            "id": str(self.id),
            "stage_id": str(self.stage_id),
            "stage_name": self.stage_name,
            "timestamp": self.timestamp,
            "status": self.status.value,
            "date": self.date.isoformat(),
            "artifact": self.artifact,
            "order": self.order,
            "finished": self.finished.isoformat() if self.finished else None,
            "details": self.details,
        }

    @classmethod
    def init_from(cls, data: dict, path: str | None = None) -> StageBuild:
        return cls(
            id=uuid.UUID(data["id"]),
            stage_id=uuid.UUID(data["stage_id"]),
            stage_name=data["stage_name"],
            timestamp=data["timestamp"],
            status=StageBuildStatus(data["status"]),
            date=datetime.fromisoformat(data["date"]),
            path=path,
            artifact=data.get("artifact"),
            order=data.get("order"),
            finished=datetime.fromisoformat(data["finished"]) if data.get("finished") else None,
            details=data.get("details") or {},
        )

    @property
    def failure_summary(self) -> str | None:
        """Failed packages and reason, eg. "net-libs/webkit-gtk-2.54.0 (compile phase), out of memory"."""
        failure = self.details.get("failure") or {}
        packages = ", ".join(f"{item['package']} ({item['phase']} phase)" for item in failure.get("packages", []))
        reason = failure.get("reason")
        return ", ".join(part for part in (packages, reason) if part) or None

    @property
    def artifact_path(self) -> str | None:
        return os.path.join(self.path, self.artifact) if self.path and self.artifact else None

    def save(self, project_directory):
        self.path = self.path or os.path.join(stage_builds_directory(project_directory, self.stage_id), self.timestamp)
        os.makedirs(self.path, exist_ok=True)
        with open(os.path.join(self.path, StageBuild.METADATA_FILE), "w", encoding="utf-8") as file:
            json.dump(self.serialize(), file, indent=4)

    def delete(self):
        """Removes build directory with its results. Directory of stage builds is removed too when it becomes empty."""
        if not self.path:
            return
        if os.path.islink(self.path):
            raise RuntimeError(f"Refusing to delete linked build directory: {self.path}")
        root = os.path.realpath(builds_location())
        path = os.path.realpath(self.path)
        if not _is_builds_descendant(path, root):
            raise RuntimeError(f"Build directory {path} is outside of builds location")
        shutil.rmtree(path)
        stage_directory = os.path.dirname(path)
        if os.path.isdir(stage_directory) and not os.listdir(stage_directory):
            os.rmdir(stage_directory)

def builds_location() -> str:
    return resolve_path(Repository.Settings.value.builds_location)

def _is_builds_descendant(path: str, root: str) -> bool:
    """True only for a path strictly below the builds root, including across platform path boundaries."""
    try:
        return path != root and os.path.commonpath((root, path)) == root
    except ValueError:
        return False

def project_builds_directory(project_directory) -> str:
    return os.path.join(builds_location(), project_directory.id.hex)

def delete_project_builds(project_directory):
    """Removes all build output, logs, project caches and run records of a project."""
    root = os.path.realpath(builds_location())
    path = project_builds_directory(project_directory)
    real_path = os.path.realpath(path)
    if os.path.islink(path) or not _is_builds_descendant(real_path, root):
        raise RuntimeError(f"Project builds directory is outside the configured builds location: {real_path}")
    if os.path.isdir(real_path):
        shutil.rmtree(real_path)

def stage_builds_directory(project_directory, stage_id: uuid.UUID) -> str:
    return os.path.join(project_builds_directory(project_directory), "stages", stage_id.hex)

def load_project_builds(project_directory) -> list[StageBuild]:
    """All builds of project stages, newest first. Builds are matched to stages by id, so renamed stages keep them."""
    builds = []
    root = os.path.join(project_builds_directory(project_directory), "stages")
    if not os.path.isdir(root):
        return builds
    for stage_directory in os.scandir(root):
        if not stage_directory.is_dir():
            continue
        for build_directory in os.scandir(stage_directory.path):
            metadata_path = os.path.join(build_directory.path, StageBuild.METADATA_FILE)
            try:
                with open(metadata_path, encoding="utf-8") as file:
                    builds.append(StageBuild.init_from(json.load(file), path=build_directory.path))
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
                if os.path.exists(metadata_path):
                    print(f"Warning: Failed to read build {metadata_path}: {e}")
    return sorted(builds, key=lambda build: build.date, reverse=True)

class BuildRunResult(Enum):
    """Result of build run (stages built together), for status of project."""
    COMPLETED = "completed"
    FAILED = "failed"   # Building of some stage failed.
    STOPPED = "stopped" # Canceled, or interrupted (app was closed).

def last_build_run(project_directory, running_timestamp: str | None = None) -> tuple[str | None, BuildRunResult | None]:
    """Timestamp and result of newest build run of project, (None, None) when project has no builds."""
    builds = load_project_builds(project_directory)
    if not builds:
        return None, None
    # load_project_builds already sorts newest first, so the first record is enough.
    timestamp = builds[0].timestamp
    statuses = {build.status for build in builds if build.timestamp == timestamp}
    # Stages are skipped when stage or step they depend on failed (eg. seed download), run failed then too.
    if statuses & {StageBuildStatus.FAILED, StageBuildStatus.SKIPPED}:
        result = BuildRunResult.FAILED
    elif timestamp != running_timestamp and statuses & {StageBuildStatus.STOPPED, StageBuildStatus.CANCELED,
                                                        StageBuildStatus.IN_PROGRESS, StageBuildStatus.SCHEDULED}:
        result = BuildRunResult.STOPPED
    else:
        result = BuildRunResult.COMPLETED
    return timestamp, result

# ------------------------------------------------------------------------------
# Build plan:

class StageBuildMode(Enum):
    NONE = "none"           # Not used in this build.
    BUILD = "build"         # Selected to be built.
    REQUIRED = "required"   # Needed as seed by selected stage and has no usable build, so it's built too.
    REUSE = "reuse"         # Needed as seed by selected stage, its previous build is used.

@dataclass
class StageBuildPlanEntry:
    stage: object
    mode: StageBuildMode
    reused_build: StageBuild | None = None # For REUSE.
    required_by: list = field(default_factory=list) # Selected stages that need this one as seed (REQUIRED, REUSE).

    @property
    def is_built(self) -> bool:
        return self.mode in (StageBuildMode.BUILD, StageBuildMode.REQUIRED)

class StageBuildPlan:
    """Decides what happens with every stage of project when building selected stages. Stage needs its parent built
    first: either reused from previous build, or built in the same run. Parents without usable builds are built too,
    up to the root of branch."""

    def __init__(self, project_directory, selected_stage_ids: set[uuid.UUID], builds: list[StageBuild] | None = None):
        self.project_directory = project_directory
        self.builds = (load_project_builds(project_directory) if builds is None
                       else sorted(builds, key=lambda build: build.date, reverse=True))
        self.entries: dict[uuid.UUID, StageBuildPlanEntry] = {
            stage.id: StageBuildPlanEntry(stage=stage, mode=StageBuildMode.BUILD if stage.id in selected_stage_ids else StageBuildMode.NONE)
            for stage in project_directory.stages
        }
        for stage_id in selected_stage_ids:
            if stage_id in self.entries:
                self._resolve_ancestors(self.entries[stage_id].stage)

    def latest_build(self, stage) -> StageBuild | None:
        """Latest build that can be used as seed (completed, with its archive still present)."""
        return next((
            build for build in self.builds
            if build.stage_id == stage.id and build.is_usable and build.artifact_path and os.path.isfile(build.artifact_path)
        ), None)

    def latest_attempt(self, stage) -> StageBuild | None:
        """Latest build, including failed and unfinished ones (not stages that didn't start)."""
        return next((build for build in self.builds if build.stage_id == stage.id and build.status.is_attempt), None)

    def parent(self, stage):
        parent_id = getattr(stage, StageArgumentDetails.parent.name, None)
        return self.entries[parent_id].stage if parent_id in self.entries else None

    def _resolve_ancestors(self, selected_stage):
        stage, visited = selected_stage, {selected_stage.id}
        while (parent := self.parent(stage)) is not None and parent.id not in visited:
            visited.add(parent.id)
            entry = self.entries[parent.id]
            if entry.mode == StageBuildMode.BUILD:
                return # Built in this run, its own ancestors are resolved when processing it.
            entry.required_by.append(selected_stage)
            if entry.mode in (StageBuildMode.REUSE, StageBuildMode.REQUIRED):
                return # Already resolved for other selected stage.
            if latest := self.latest_build(parent):
                entry.mode = StageBuildMode.REUSE
                entry.reused_build = latest
                return # Previous build is used as seed, no need to go further.
            entry.mode = StageBuildMode.REQUIRED
            stage = parent

    def build_order(self) -> list:
        """Stages built parent-first; raise ValueError for a malformed parent cycle."""
        order, added, visiting = [], set(), set()
        def add(stage):
            if stage.id in added:
                return
            if stage.id in visiting:
                raise ValueError(f"Stage parent cycle includes {stage.name}")
            visiting.add(stage.id)
            if (parent := self.parent(stage)) is not None and self.entries[parent.id].is_built:
                add(parent)
            visiting.remove(stage.id)
            added.add(stage.id)
            order.append(stage)
        for entry in self.entries.values():
            if entry.is_built:
                add(entry.stage)
        return order
