from __future__ import annotations
import os, sys
from datetime import datetime, timezone
from enum import Enum
from gi.repository import Gtk, Adw, GLib, Gio
from .project_directory import ProjectDirectory
from .project_build import StageBuildStatus, load_project_builds
from .project_build_record import load_build_record, delete_build_record, failed_steps
from .multistage_process_execution_view import MultistageProcessExecutionView
from .project_build_process import ProjectBuild, ProjectBuildStepBuildStage, running_project_build
from .project_build_view import ProjectBuildView
from .build_queue_view import build_button_state, present_queued_build
from .build_queue import BuildQueue, BuildQueueEvent
from .multistage_process import MultiStageProcess, MultiStageProcessEvent, MultiStageProcessStageEvent, MultiStageProcessStageState, MultiStageProcessState
from .app_events import app_event_bus, AppEvents
from .helper_functions import get_file_size_string
from .project_build_details import failure_details, format_duration
from .deploy_installation import is_deployable
from .deploy_live_iso import is_live_iso_build
from .deploy_create_view import DeployCreateView

class BuildRowState(Enum):
    """State of stage in build run, as displayed."""
    COMPLETED = "Completed"
    FAILED = "Failed"
    BUILDING = "Building"
    SCHEDULED = "Scheduled"
    SKIPPED = "Skipped" # Stage it depends on failed.
    CANCELLED = "Cancelled" # Build was cancelled before stage started.
    STOPPED = "Stopped" # Build was cancelled while stage was building.
    INTERRUPTED = "Interrupted" # Not finished, but its build is not running anymore (eg. app was closed).
    NOT_STARTED = "Not started" # Scheduled, but its build is not running anymore (eg. app was closed).

class ProjectBuildsView(Gtk.Box):
    """Lists builds of project stages, grouped by build runs (stages built together). Shown from Builds section.
    Running build includes its scheduled stages, and its rows open build progress."""

    def __init__(self, project_directory: ProjectDirectory, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.project_directory = project_directory
        self.content_navigation_view = content_navigation_view
        # Same layout as other views: groups use whole width of window.
        scrolled_window = Gtk.ScrolledWindow(hexpand=True, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_start=24, margin_end=24, margin_top=6, margin_bottom=24)
        scrolled_window.set_child(self.content)
        self.append(scrolled_window)
        # Starts new build of project, or shows progress of running one (like Create build in project page).
        actions_group = Adw.PreferencesGroup()
        self.build_row = Adw.ButtonRow(title="Start new build", start_icon_name="sledgehammer-svgrepo-com-symbolic")
        self.build_row.connect("activated", self._on_build_activated)
        actions_group.add(self.build_row)
        self.content.append(actions_group)
        self.groups: list[Adw.PreferencesGroup] = []
        self._observed_build = None
        self._reload_scheduled = False
        # Builds can change while view is open (build finished), refresh when shown again.
        self.connect("map", lambda widget: self.load_builds())
        MultiStageProcess.event_bus.subscribe(MultiStageProcessEvent.STARTED_PROCESSES_CHANGED, self._on_started_processes_changed)
        BuildQueue.shared().event_bus.subscribe(BuildQueueEvent.CHANGED, self._on_started_processes_changed)
        from .qemu_run import QemuRun, QemuRunEvent
        QemuRun.running_runs_event_bus.subscribe(QemuRunEvent.RUNNING_RUNS_CHANGED,
                                                 self._on_started_processes_changed, self)

    # Loading builds
    # --------------------------------------------------------------------------

    def load_builds(self):
        # Result of newest build is seen when builds are displayed (opened, or build finished while open).
        if self.get_mapped() or not getattr(self, "_result_seen", False):
            self._result_seen = True
            self.project_directory.mark_build_result_seen()
        for group in self.groups:
            self.content.remove(group)
        self.groups = []
        running_build = running_project_build(self.project_directory)
        self._observe_running_build(running_build)
        self._update_build_row(running_build)
        builds = load_project_builds(self.project_directory)
        if not builds and not running_build:
            group = Adw.PreferencesGroup()
            group.add(Adw.StatusPage(
                icon_name="box-minimalistic-svgrepo-com-symbolic",
                title="No builds yet",
                description="Build stages of this project from its Stages page."
            ))
            self._add_group(group)
            return
        # Stages built together share timestamp, group them as one build run. Newest runs first (builds are loaded
        # newest first). Running build is always first and contains also its stages that didn't start yet.
        stage_ids = {stage.id for stage in self.project_directory.stages}
        runs: dict[str, list] = {}
        if running_build:
            runs[running_build.timestamp] = []
        for build in builds:
            runs.setdefault(build.timestamp, []).append(build)
        for timestamp, run_builds in runs.items():
            if running_build and timestamp == running_build.timestamp:
                entries = self._running_build_entries(running_build, run_builds)
            else:
                # Stages in build order (older records without it by date).
                ordered = sorted(run_builds, key=lambda build: (build.order is None, build.order or 0, build.date))
                entries = [(build.stage_name, build, _record_state(build)) for build in ordered]
            is_running = running_build is not None and timestamp == running_build.timestamp
            title = f"Started {_format_timestamp(timestamp, fallback=entries[0][1].date if entries and entries[0][1] else datetime.now())}"
            description = _run_summary([state for _, _, state in entries])
            # Failed steps that don't build stages (eg. seed download) are why stages were skipped.
            if is_running:
                failed = [step.name for step in running_build.stages
                          if not isinstance(step, ProjectBuildStepBuildStage) and step.state == MultiStageProcessStageState.FAILED]
            else:
                failed = failed_steps(self.project_directory, timestamp)
            if failed:
                description += "\n" + ", ".join(failed) + " failed"
            group = Adw.PreferencesGroup(title=title, description=GLib.markup_escape_text(description))
            if not is_running:
                group.set_header_suffix(self._delete_button(
                    tooltip="Remove builds",
                    heading="Remove builds?",
                    body=(
                        f"All {len(run_builds)} builds started {title.removeprefix('Started ')} will be removed with their files. This can't be undone."
                        if len(run_builds) > 1 else
                        f"Build started {title.removeprefix('Started ')} will be removed with its files. This can't be undone."
                    ),
                    builds=run_builds,
                    timestamp=timestamp
                ))
            for stage_name, build, state in entries:
                stage_removed = build is not None and build.stage_id not in stage_ids
                # Rows open steps of their run, like build progress (also of runs finished before app was started).
                group.add(self._build_row(stage_name, build, state, stage_removed, running_build if is_running else None,
                                          on_activated=lambda timestamp=timestamp, run_builds=run_builds: self._show_run_steps(timestamp, run_builds)))
            self._add_group(group)

    def _running_build_entries(self, running_build, run_builds: list) -> list[tuple]:
        """Stages of running build in build order, with builds of the ones that started."""
        builds_by_stage = {build.stage_id: build for build in run_builds}
        steps_by_stage = {step.stage.id: step for step in running_build.stages if isinstance(step, ProjectBuildStepBuildStage)}
        entries = []
        for stage in running_build.plan.build_order():
            step = steps_by_stage.get(stage.id)
            # Step reports its state before its build record is saved, so state of running build is taken from steps,
            # records give details (and results of finished stages).
            build = (step.build if step is not None else None) or builds_by_stage.get(stage.id)
            step_state = step.state if step is not None else None
            if step_state == MultiStageProcessStageState.IN_PROGRESS:
                state = BuildRowState.BUILDING
            elif build is not None and build.status.is_attempt and build.status != StageBuildStatus.IN_PROGRESS:
                state = _record_state(build)
            elif step_state == MultiStageProcessStageState.FAILED:
                state = BuildRowState.SKIPPED # Failed without starting build: stage it depends on failed.
            else:
                state = BuildRowState.SCHEDULED
            entries.append((stage.name, build, state))
        return entries

    def _add_group(self, group: Adw.PreferencesGroup):
        self.content.append(group)
        self.groups.append(group)

    def _build_row(self, stage_name: str, build, state: BuildRowState, stage_removed: bool, running_build,
                   on_activated=None) -> Adw.ActionRow:
        details = [state.value]
        if stage_removed:
            details.append("stage was removed from project")
        if build and build.duration:
            details.append(format_duration(build.duration))
        if build and build.artifact_path and os.path.isfile(build.artifact_path):
            details.append(build.artifact)
            if size := get_file_size_string(build.artifact_path):
                details.append(size)
            if packages := (build.details.get("output") or {}).get("packages"):
                details.append(f"{packages} packages")
        # ISO of livecd stage, in build folder next to stage archive (builds made before ISO became their artifact).
        if build and build.path and (iso := (build.details.get("output") or {}).get("iso")) and iso != build.artifact:
            iso_path = os.path.join(build.path, iso)
            if os.path.isfile(iso_path):
                details.append(iso)
                if size := get_file_size_string(iso_path):
                    details.append(size)
        subtitle = " · ".join(details)
        if state == BuildRowState.FAILED and (failure := _failure_summary(build)):
            subtitle += f"\n{failure}"
        row = Adw.ActionRow(title=GLib.markup_escape_text(stage_name), subtitle=GLib.markup_escape_text(subtitle))
        if build and (build.details.get("failure") or {}).get("logs"):
            row.set_tooltip_text("Details of failure are in failure folder of build: summary.txt with errors of failed packages, their logs, emerge --info and configuration of stage")
        icon = Gtk.Image.new_from_icon_name(_state_icon(state))
        icon.set_pixel_size(24)
        if css_class := _state_css_class(state):
            icon.add_css_class(css_class)
        if state == BuildRowState.SKIPPED:
            icon.set_tooltip_text("Skipped because a required stage failed")
        row.add_prefix(icon)

        actions = Gtk.Box(spacing=0, valign=Gtk.Align.CENTER)

        def action_slot(button=None, tooltip: str | None = None):
            slot = Gtk.Box(width_request=40, height_request=36, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            if tooltip:
                slot.set_tooltip_text(tooltip)
            if button is not None:
                slot.append(button)
            actions.append(slot)

        # Keep the same action positions in every build row: VM, deploy, folder, remove, details.
        qemu_button = None
        qemu_tooltip = None
        if build and not running_build:
            from .qemu_run import QemuRun, qemu_support
            from .apple_virtualization import apple_support
            support = qemu_support(self.project_directory, build)
            apple = apple_support(self.project_directory, build)
            available = support.available or apple.available
            running_qemu = QemuRun.running_for_build(self.project_directory, build)
            if support.kind is not None or running_qemu:
                other_vm_running = any(run.is_running and run is not running_qemu for run in QemuRun.running_runs)
                qemu_tooltip = (
                    "Another virtual machine is running. Stop it before starting this one."
                    if other_vm_running else
                    "Open running virtual machine" if running_qemu else
                    "Run built image" if available else
                    (f"QEMU: {support.reason}\nApple Virtualization: {apple.reason}" if sys.platform == "darwin"
                     else f"Can't run in QEMU: {support.reason}")
                )
                qemu_button = Gtk.Button(icon_name="computer-symbolic", valign=Gtk.Align.CENTER)
                qemu_button.add_css_class("flat")
                qemu_button.set_sensitive(not other_vm_running and (available or running_qemu is not None))
                if not available and not running_qemu:
                    qemu_button.add_css_class("dimmed")
                qemu_button.connect("clicked", lambda _, build=build: self._run_in_qemu(build))
        action_slot(qemu_button, qemu_tooltip)

        deploy_button = None
        if build and not running_build and is_deployable(self.project_directory, build):
            deploy_button = Gtk.Button(icon_name="deploy-symbolic", valign=Gtk.Align.CENTER)
            deploy_button.add_css_class("flat")
            deploy_button.set_tooltip_text("Deploy on another machine")
            deploy_button.connect("clicked", lambda _, build=build: app_event_bus.emit(
                AppEvents.PRESENT_VIEW, DeployCreateView(project_directory=self.project_directory, build=build), "Deploy build", 640, 560))
        elif build and not running_build and is_live_iso_build(build) and not is_deployable(self.project_directory, build):
            deploy_button = Gtk.Button(icon_name="deploy-symbolic", valign=Gtk.Align.CENTER)
            deploy_button.add_css_class("flat")
            deploy_button.set_tooltip_text("Write to disk or save as ISO for other machine")
            deploy_button.connect("clicked", lambda _, build=build: self._deploy_live_iso(build))
        action_slot(deploy_button)

        folder_button = None
        if build and build.path and os.path.isdir(build.path):
            folder_button = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
            folder_button.add_css_class("flat")
            folder_button.set_tooltip_text("Open build folder")
            folder_button.connect("clicked", lambda _, path=build.path: Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(self.get_root(), None, None))
        action_slot(folder_button)

        delete_button = None
        if build and not running_build:
            delete_button = self._delete_button(
                tooltip="Remove build",
                heading="Remove build?",
                body=f"Build of \"{stage_name}\" will be removed with its files. This can't be undone.",
                builds=[build]
            )
        action_slot(delete_button)

        if on_activated is not None:
            row.set_activatable(True)
            row.connect("activated", lambda _: on_activated())
            arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
            arrow.add_css_class("dimmed")
            action_slot(arrow)
        else:
            action_slot()

        row.add_suffix(actions)
        return row

    # Removing builds
    # --------------------------------------------------------------------------

    def _run_in_qemu(self, build):
        from .qemu_run import QemuRun
        from .qemu_run_view import present_qemu_run
        if run := QemuRun.running_for_build(self.project_directory, build):
            present_qemu_run(run)
            return
        from .qemu_launch_wizard import QemuLaunchWizard
        app_event_bus.emit(AppEvents.PRESENT_VIEW, QemuLaunchWizard(self.project_directory, build),
                           f"Run {build.stage_name}", 1024, 768)

    def _deploy_live_iso(self, build):
        from .live_iso_deploy_view import LiveIsoDeployView
        app_event_bus.emit(AppEvents.PRESENT_VIEW, LiveIsoDeployView(project_directory=self.project_directory, build=build),
                           "Deploy Live CD", 640, 560)

    def _delete_button(self, tooltip: str, heading: str, body: str, builds: list, timestamp: str | None = None) -> Gtk.Button:
        """Removes builds, and record of run with given timestamp (whole run is removed)."""
        button = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=tooltip, valign=Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.connect("clicked", lambda _: self._confirm_delete(heading, body, builds, timestamp))
        return button

    def _confirm_delete(self, heading: str, body: str, builds: list, timestamp: str | None = None):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Remove")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _, response: self._delete_builds(builds, timestamp) if response == "delete" else None)
        dialog.present(self.get_root())

    def _delete_builds(self, builds: list, timestamp: str | None = None):
        for build in builds:
            try:
                build.delete()
            except Exception as e:
                print(f"Error removing build {build.path}: {e}")
        if timestamp is not None:
            delete_build_record(self.project_directory, timestamp)
        self.load_builds()

    def _show_run_steps(self, timestamp: str, builds: list):
        """Build progress of run: running or finished in this session (with all its output), or from its record."""
        process = next((build for build in MultiStageProcess.get_started_processes_by_class(ProjectBuild)
                        if build.project_directory.id == self.project_directory.id and build.timestamp == timestamp), None)
        if process is not None:
            self._open_build_progress(process)
            return
        view = MultistageProcessExecutionView()
        view.set_multistage_process(load_build_record(self.project_directory, timestamp, builds))
        app_event_bus.emit(AppEvents.PRESENT_VIEW, view, "Build stages", 640, 480)

    def _update_build_row(self, running_build):
        """Only one build runs at a time (also of different projects), toolset can't be busy with other operation."""
        if running_build:
            # Plain row, like build progress row in project page.
            self.build_row.set_title("Show build progress")
            self.build_row.remove_css_class("suggested-action")
            self.build_row.set_sensitive(True)
            self.build_row.set_tooltip_text(None)
            return
        self.build_row.add_css_class("suggested-action")
        # Builds of other projects are added to queue while one is built.
        state = build_button_state(self.project_directory)
        self.build_row.set_title("Start new build" if state.title == "Create build" else state.title)
        self.build_row.set_sensitive(state.sensitive)
        self.build_row.set_tooltip_text(state.tooltip)

    def _on_build_activated(self, row):
        if running_build := running_project_build(self.project_directory):
            self._open_build_progress(running_build)
            return
        project = self.project_directory
        if error := project.configuration_error:
            dialog = Adw.AlertDialog(heading="Can't start build", body=error)
            dialog.add_response("ok", "OK")
            dialog.present(self.get_root())
            return
        if (queued := build_button_state(project).queued) is not None:
            present_queued_build(queued)
            return
        app_event_bus.emit(AppEvents.PRESENT_VIEW, ProjectBuildView(project_directory=project), "Build stages", 640, 480)

    def _open_build_progress(self, running_build):
        app_event_bus.emit(
            AppEvents.PRESENT_VIEW,
            ProjectBuildView(project_directory=self.project_directory, installation_in_progress=running_build),
            "Build stages", 640, 480
        )

    # Refreshing while build is running
    # --------------------------------------------------------------------------

    def _observe_running_build(self, running_build):
        if running_build is None or running_build is self._observed_build:
            return
        self._observed_build = running_build
        for step in running_build.stages:
            step.event_bus.subscribe(MultiStageProcessStageEvent.STATE_CHANGED, self._on_build_changed)

    def _on_build_changed(self, *args):
        self._schedule_reload()

    def _on_started_processes_changed(self, *args):
        self._schedule_reload()

    def _schedule_reload(self):
        # Several changes can come at once, reload once.
        if self._reload_scheduled or not self.get_mapped():
            return
        self._reload_scheduled = True
        def reload():
            self._reload_scheduled = False
            self.load_builds()
            return False
        GLib.idle_add(reload)

def _record_state(build) -> BuildRowState:
    match build.status:
        case StageBuildStatus.COMPLETED: return BuildRowState.COMPLETED
        case StageBuildStatus.FAILED: return BuildRowState.FAILED
        case StageBuildStatus.SKIPPED: return BuildRowState.SKIPPED
        case StageBuildStatus.CANCELLED: return BuildRowState.CANCELLED
        case StageBuildStatus.STOPPED: return BuildRowState.STOPPED
        case StageBuildStatus.SCHEDULED: return BuildRowState.NOT_STARTED
    # Running builds are handled separately, so not finished build here is not running anymore.
    return BuildRowState.INTERRUPTED

def _format_timestamp(timestamp: str, fallback: datetime) -> str:
    """Build run timestamp (@TIMESTAMP@, in UTC) in local time."""
    try:
        date = datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone()
    except ValueError:
        date = fallback
    return date.strftime("%Y-%m-%d %H:%M")

def _run_summary(states: list[BuildRowState]) -> str:
    parts = [f"{len(states)} stage{'s' if len(states) != 1 else ''}"]
    for state in BuildRowState:
        if count := states.count(state):
            parts.append(f"{count} {state.value.lower()}")
    return ", ".join(parts)

def _state_icon(state: BuildRowState) -> str:
    match state:
        case BuildRowState.COMPLETED: return "check-square-svgrepo-com-symbolic"
        case BuildRowState.FAILED | BuildRowState.INTERRUPTED: return "error-box-svgrepo-com-symbolic"
        case BuildRowState.BUILDING: return "menu-dots-square-svgrepo-com-symbolic"
        case BuildRowState.CANCELLED | BuildRowState.STOPPED: return "stop-square-symbolic"
        case BuildRowState.SKIPPED: return "dialog-warning-symbolic"
        case BuildRowState.NOT_STARTED: return "square-svgrepo-com-symbolic"
        case _: return "clock-square-svgrepo-com-symbolic"

def _state_css_class(state: BuildRowState) -> str | None:
    match state:
        case BuildRowState.COMPLETED: return "success"
        case BuildRowState.FAILED | BuildRowState.INTERRUPTED: return "error"
        case BuildRowState.BUILDING: return "accent"
        case BuildRowState.SKIPPED: return "warning"
        case BuildRowState.SCHEDULED | BuildRowState.CANCELLED | BuildRowState.STOPPED | BuildRowState.NOT_STARTED: return "dimmed"
    return None

def _failure_summary(build) -> str | None:
    """Failed packages and reason. Builds made before details were recorded have them read from build.log."""
    if build is None:
        return None
    if "failure" not in build.details and build.path:
        try:
            with open(os.path.join(build.path, "build.log"), encoding="utf-8", errors="replace") as file:
                build.details["failure"] = failure_details(file.read().splitlines(), None)
        except OSError:
            return None
    return build.failure_summary
