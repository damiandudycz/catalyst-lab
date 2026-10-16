from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Callable
from gi.repository import GLib
from .event_bus import EventBus
from .multistage_process import MultiStageProcess, MultiStageProcessEvent, MultiStageProcessState

# ------------------------------------------------------------------------------
# Queue of builds of other projects, started one after another when running build finishes (completed or failed).
# Builds are planned when they start (they can use stages built by previous builds). Every project can be queued
# once, and not while it's being built. The queue exists only for the current app session.

class BuildQueueEvent(Enum):
    CHANGED = auto()

LATEST = "latest" # Snapshot option: latest snapshot is generated before building.

@dataclass
class QueuedBuild:
    project_id: uuid.UUID
    stage_ids: list[uuid.UUID]
    snapshot: str | None # LATEST, filename of snapshot, or None (snapshot of project).
    queued: datetime = field(default_factory=datetime.now)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def project_directory(self):
        from .repository import Repository
        return next((project for project in Repository.ProjectDirectory.value if project.id == self.project_id), None)

    @property
    def stage_names(self) -> list[str]:
        project = self.project_directory
        stages = {stage.id: stage.name for stage in project.stages} if project else {}
        return [stages[stage_id] for stage_id in self.stage_ids if stage_id in stages]

def running_build():
    """Build in progress (of any project)."""
    from .project_build_process import ProjectBuild
    return next((build for build in MultiStageProcess.get_started_processes_by_class(ProjectBuild)
                 if build.status == MultiStageProcessState.IN_PROGRESS), None)

class BuildQueue:
    _instance = None

    @classmethod
    def shared(cls) -> BuildQueue:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.event_bus = EventBus[BuildQueueEvent]()
        self.entries: list[QueuedBuild] = []
        self.paused = False
        self.pause_reason: str | None = None
        self._check_scheduled = False
        self._observed: set[int] = set()
        MultiStageProcess.event_bus.subscribe(MultiStageProcessEvent.STARTED_PROCESSES_CHANGED, self._on_processes_changed)

    def _changed(self):
        self.event_bus.emit(BuildQueueEvent.CHANGED)
        self._schedule_check()

    # Entries:

    def entry_for(self, project_directory) -> QueuedBuild | None:
        return next((entry for entry in self.entries if entry.project_id == project_directory.id), None)

    def queue_unavailable_reason(self, project_directory) -> str | None:
        """Why project can't be queued: it's being built or already queued."""
        build = running_build()
        if build is not None and build.project_directory.id == project_directory.id:
            return "Project is being built"
        if self.entry_for(project_directory):
            return "Build of project is already queued"
        return None

    def add(self, project_directory, stage_ids: list[uuid.UUID], snapshot: str | None):
        if reason := self.queue_unavailable_reason(project_directory):
            raise RuntimeError(reason)
        self.entries.append(QueuedBuild(project_id=project_directory.id, stage_ids=list(stage_ids), snapshot=snapshot))
        self._changed()

    def remove(self, entry: QueuedBuild):
        if entry in self.entries:
            self.entries.remove(entry)
            self._changed()

    def move(self, entry: QueuedBuild, offset: int):
        index = self.entries.index(entry)
        target = max(0, min(len(self.entries) - 1, index + offset))
        if target != index:
            self.entries.insert(target, self.entries.pop(index))
            self._changed()

    def clear(self):
        self.entries = []
        self._changed()

    def start(self):
        self.paused = False
        self.pause_reason = None
        self._changed()

    def pause(self, reason: str | None = None):
        self.paused = True
        self.pause_reason = reason
        self._changed()

    # Starting builds:

    def _on_processes_changed(self, *args):
        # Running builds are observed, queue continues when they finish.
        build = running_build()
        if build is not None and id(build) not in self._observed:
            self._observed.add(id(build))
            build.event_bus.subscribe(MultiStageProcessEvent.STATE_CHANGED, self._on_build_state_changed)
        self._schedule_check()

    def _on_build_state_changed(self, *args):
        from .repository import Repository
        build = next((build for build in MultiStageProcess.started_processes if id(build) in self._observed
                      and build.status == MultiStageProcessState.FAILED), None)
        if build is not None and not getattr(build, "_queue_failure_handled", False):
            build._queue_failure_handled = True
            # Stopped build continues queue (unless user canceled queue too), failed one only when settings allow.
            if not getattr(build, "canceled", False) and Repository.Settings.value.pause_queue_on_failure and self.entries:
                self.pause(f"Build of {build.project_directory.name} failed")
        self._schedule_check()

    def _schedule_check(self):
        if not self._check_scheduled:
            self._check_scheduled = True
            GLib.timeout_add(500, self._check)

    def _check(self) -> bool:
        self._check_scheduled = False
        if self.paused or not self.entries or running_build() is not None:
            return False
        entry = self.entries[0]
        project = entry.project_directory
        if project is None:
            print("Queued project was removed, its build is skipped")
            self.remove(entry)
            return False
        toolset = project.get_toolset()
        if toolset is not None and toolset.is_reserved:
            # Previous build still releases toolset (or other operation uses it), checked again later.
            self._schedule_check_later()
            return False
        if reason := (project.configuration_error or (toolset.busy_reason if toolset else "Project has no toolset")):
            print(f"Queued build of {project.name} can't start: {reason}")
            self.pause(f"Build of {project.name} can't start: {reason}")
            return False
        self.entries.remove(entry)
        self._changed()
        self._start(entry, project)
        return False

    def _schedule_check_later(self):
        GLib.timeout_add_seconds(3, lambda: (self._schedule_check(), False)[1])

    def _start(self, entry: QueuedBuild, project):
        from .repository import Repository
        from .project_build import StageBuildPlan
        from .project_build_process import start_project_build
        stage_ids = {stage.id for stage in project.stages} & set(entry.stage_ids)
        if not stage_ids:
            print(f"Stages of queued build of {project.name} were removed, build is skipped")
            self._schedule_check()
            return
        plan = StageBuildPlan(project, stage_ids) # Planned now, previous builds can be reused.
        snapshot = None
        if entry.snapshot not in (None, LATEST):
            snapshot = next((item for item in Repository.Snapshot.value if item.filename == entry.snapshot), None)
        def started(build):
            if build is None:
                print(f"Queued build of {project.name} didn't start")
                self.pause(f"Build of {project.name} didn't start")
        start_project_build(project, plan, snapshot=snapshot, fetch_snapshot=entry.snapshot == LATEST, done=started)
