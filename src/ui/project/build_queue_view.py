from __future__ import annotations
from dataclasses import dataclass
from gi.repository import Gtk, GLib
from .app_events import app_event_bus, AppEvents
from .build_queue import BuildQueue, BuildQueueEvent, QueuedBuild, running_build, LATEST
from .multistage_process import MultiStageProcess, MultiStageProcessStage, MultiStageProcessState
from .multistage_process_execution_view import MultistageProcessExecutionView

# ------------------------------------------------------------------------------
# Queued build shown like build progress (its planned steps), with banner about waiting in queue. Its Cancel removes
# it from queue. When it starts, progress of started build is shown. State of build buttons of projects (create build,
# add it to queue, or show queued build).

@dataclass
class BuildButtonState:
    visible: bool
    sensitive: bool
    title: str
    tooltip: str | None
    queued: QueuedBuild | None # Project is queued, button shows its queued build.

def build_button_state(project_directory) -> BuildButtonState:
    """State of button creating build of project (hidden while project is built, its progress is shown instead)."""
    running = running_build()
    queue = BuildQueue.shared()
    if running is not None and running.project_directory.id == project_directory.id:
        return BuildButtonState(False, False, "Create build", None, None)
    if entry := queue.entry_for(project_directory):
        return BuildButtonState(True, True, "Waiting in queue", "Shows queued build", entry)
    if running is not None:
        error = project_directory.configuration_error
        return BuildButtonState(True, error is None, GLib.markup_escape_text(error) if error else "Add build to queue",
                                error or f"Starts after build of {running.project_directory.name} and queued builds", None)
    toolset = project_directory.get_toolset()
    reason = toolset.busy_reason if toolset else "Select toolset in project configuration"
    return BuildButtonState(True, reason is None, GLib.markup_escape_text(reason) if reason else "Create build", reason, None)

def present_queued_build(entry: QueuedBuild):
    app_event_bus.emit(AppEvents.PRESENT_VIEW, QueuedBuildView(entry), "Build stages", 640, 480)

class _PlannedStep(MultiStageProcessStage):
    """Step of queued build, not started yet."""

    def start(self):
        pass

class QueuedBuildPreview(MultiStageProcess):
    """Steps that queued build would run if it started now (it's planned again when it starts). Its cancel removes
    build from queue."""

    def __init__(self, entry: QueuedBuild):
        self.entry = entry
        self.project_directory = entry.project_directory
        super().__init__(title=f"Building {self.project_directory.name}" if self.project_directory else "Queued build")
        self.status = MultiStageProcessState.IN_PROGRESS # Shown like waiting build (Cancel removes it from queue).

    def name(self) -> str:
        return self.project_directory.name if self.project_directory else "Queued build"

    def setup_stages(self):
        project = self.project_directory
        if project is None:
            return
        from .project_build import StageBuildPlan
        from .project_build_process import ProjectBuild
        stage_ids = {stage.id for stage in project.stages} & set(self.entry.stage_ids)
        try:
            build = ProjectBuild(project_directory=project, plan=StageBuildPlan(project, stage_ids),
                                 fetch_snapshot=self.entry.snapshot == LATEST,
                                 cleanup_previous_builds=self.entry.cleanup_previous_builds)
            self.stages = [_PlannedStep(step.name, step.description, self) for step in build.stages]
        except Exception as e:
            print(f"Failed to plan queued build of {project.name}: {e}")

    def cancel(self):
        BuildQueue.shared().remove(self.entry)

    def complete_process(self, success: bool):
        pass

class QueuedBuildView(Gtk.Box):
    """Steps of queued build with banner about waiting in queue (or paused queue, which can be started). Shows
    progress of build when it starts, closes when build is removed from queue."""

    def __init__(self, entry: QueuedBuild):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._window = None
        self.entry = entry
        self.queue = BuildQueue.shared()
        # Line about waiting in queue, like line about template in project page.
        self.banner = Gtk.Box(spacing=8, margin_start=24, margin_end=24, margin_top=6, margin_bottom=6)
        self.banner_label = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.banner_label.add_css_class("caption")
        self.banner_label.add_css_class("dimmed")
        self.banner_button = Gtk.Button(label="Start queue", valign=Gtk.Align.CENTER)
        self.banner_button.add_css_class("caption")
        self.banner_button.add_css_class("small-button")
        self.banner_button.connect("clicked", lambda button: self.queue.start())
        self.banner.append(self.banner_label)
        self.banner.append(self.banner_button)
        self.append(self.banner)
        self.execution_view = MultistageProcessExecutionView(vexpand=True)
        self.execution_view.set_multistage_process(QueuedBuildPreview(entry))
        self.execution_view._window = _Closer(self)
        self.append(self.execution_view)
        self.queue.event_bus.subscribe(BuildQueueEvent.CHANGED, self._on_queue_changed)
        self._update_banner()

    def _update_banner(self):
        entries = self.queue.entries
        position = entries.index(self.entry) + 1 if self.entry in entries else 0
        running = running_build()
        if self.queue.paused:
            reason = f": {self.queue.pause_reason}" if self.queue.pause_reason else ""
            self.banner_label.set_label(f"Waiting in queue, queue is paused{reason}")
            self.banner_button.set_visible(True)
        else:
            waiting = f"after build of {running.project_directory.name}" if running is not None else "to start"
            ahead = f" and {position - 1} queued build{'s' if position > 2 else ''}" if position > 1 else ""
            self.banner_label.set_label(f"Waiting in queue {waiting}{ahead}")
            self.banner_button.set_visible(False)

    def _on_queue_changed(self, *args):
        if self.entry in self.queue.entries:
            self._update_banner()
            return
        # Build started (its progress is shown), or it was removed from queue (view is closed).
        GLib.timeout_add(300, self._show_started_build)

    def _show_started_build(self) -> bool:
        from .project_build_process import running_project_build
        project = self.entry.project_directory
        build = running_project_build(project) if project else None
        if build is None:
            self.close()
            return False
        self.banner.set_visible(False)
        self.remove(self.execution_view)
        self.execution_view = MultistageProcessExecutionView(vexpand=True)
        self.execution_view.set_multistage_process(build)
        self.execution_view._window = _Closer(self)
        self.append(self.execution_view)
        return False

    def close(self):
        if self._window:
            self._window.close()

class _Closer:
    """Window of execution view inside queued build view: closing it closes view."""

    def __init__(self, view: QueuedBuildView):
        self.view = view

    def close(self):
        self.view.close()
