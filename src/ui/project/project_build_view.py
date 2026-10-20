from __future__ import annotations
import os
import shutil
import tempfile
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from gi.repository import Gtk, Adw, GLib, Gio
from .project_directory import ProjectDirectory
from .project_build import StageBuildPlan, StageBuildMode, load_project_builds, project_builds_directory
from .project_build_process import ProjectBuild, start_project_build
from .build_queue import BuildQueue, running_build, LATEST
from .project_stage import stage_target_icon
from .multistage_process import MultiStageProcessState, MultiStageProcessEvent
from .wizard_view import WizardView
from .repository import Repository, resolve_path
from .item_select_view import ItemSelectionViewEvent
from .toolset_application import ToolsetApplication
from .toolset_selection import sorted_toolsets, toolset_has_applications, build_toolset_requirements, default_toolset
from .snapshot import LATEST_SNAPSHOT
from .tree_node import walk_tree
from .project_build_spec import StageSpecContext, missing_required_stage_values
from .project_stage_arguments import StageArgumentDetails
from .stages_tree_view import StageNodeStatus
from .runner import default_runner, RunnerType, runner_is_supported
from .event_bus import SharedEvent

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/project/project_build_view.ui')
class ProjectBuildView(Gtk.Box):
    """Wizard for building project stages. Parents of selected stages are reused from previous builds when possible,
    otherwise they are built too."""
    __gtype_name__ = "ProjectBuildView"

    # Main views:
    wizard_view = Gtk.Template.Child()
    # Setup view elements:
    stages_page = Gtk.Template.Child()
    toolset_page = Gtk.Template.Child()
    toolset_selection_view = Gtk.Template.Child()
    runner_selection_view = Gtk.Template.Child()
    snapshot_page = Gtk.Template.Child()
    snapshot_selection_view = Gtk.Template.Child()
    summary_page = Gtk.Template.Child()
    generated_files_page = Gtk.Template.Child()
    stages_group = Gtk.Template.Child()
    build_order_group = Gtk.Template.Child()
    reused_group = Gtk.Template.Child()
    location_row = Gtk.Template.Child()
    runs_on_row = Gtk.Template.Child()
    snapshot_row = Gtk.Template.Child()
    cleanup_previous_builds_row = Gtk.Template.Child()
    generated_files_group = Gtk.Template.Child()
    generated_files_info_group = Gtk.Template.Child()
    generated_files_location_row = Gtk.Template.Child()

    def __init__(self, project_directory: ProjectDirectory, installation_in_progress: ProjectBuild | None = None, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__()
        self.project_directory = project_directory
        self.installation_in_progress = installation_in_progress
        self.content_navigation_view = content_navigation_view
        self.selected_stage_ids: set = set()
        self._requirement_statuses = {}
        self._requirement_generation = 0
        self._requirement_cancel = None
        self._requirement_future = None
        self._requirement_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="build-stage-requirements")
        self.required_toolset_apps = build_toolset_requirements(project_directory)
        self._monitored_toolsets = []
        toolsets = sorted_toolsets(applications=self.required_toolset_apps)
        from .project_toolsets import get as get_project_toolset
        preferred_id = get_project_toolset(project_directory.directory_path())
        preferred = next((item for item in toolsets if str(item.uuid) == str(preferred_id)), None)
        default = default_toolset(applications=self.required_toolset_apps, toolsets=toolsets)
        ordered = []
        for item in (preferred, default, *toolsets):
            if item is not None and item not in ordered:
                ordered.append(item)
        self.toolset_selection_view.preferred_item = next(
            (item for item in ordered if toolset_has_applications(item, self.required_toolset_apps)
             and item.busy_reason is None),
            None,
        )
        self.toolset_selection_view.set_static_list(ordered)
        self.toolset_selection_view.event_bus.subscribe(ItemSelectionViewEvent.ITEM_CHANGED, self._toolset_changed)
        self.runner_selection_view.preferred_item = default_runner()
        self.runner_selection_view.event_bus.subscribe(ItemSelectionViewEvent.ITEM_CHANGED, self._runner_changed)
        self.builds = load_project_builds(project_directory)
        self.rows: dict = {} # stage id -> (row, check_button)
        self.summary_rows: list = []
        self._updating = False
        self._preview_root = None
        self._preview_timestamp = None
        self._preview_rows = []
        self._preview_generation = 0
        self._preview_ready = False
        self._preview_cancel = None
        self._preview_future = None
        self._preview_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="build-file-preview")
        self._preview_back_confirmed = False
        self.snapshot_selection_view.event_bus.subscribe(ItemSelectionViewEvent.ITEM_CHANGED, self.snapshot_changed)
        self.load_snapshots()
        self.wizard_view.back_interceptor = self._intercept_preview_back
        self.wizard_view.carousel.connect("page-changed", self._wizard_page_changed)
        self.connect("realize", self.on_realize)
        # While other project is being built, build is added to queue (it starts when previous builds finish).
        if installation_in_progress is None and self._queue_mode():
            self.wizard_view.install_button_label = "Add to queue"
        # Result of build is seen when it finishes while this view is shown, or when view of finished build is shown.
        self._observed_build = None
        self.connect("map", lambda widget: self._mark_result_seen())
        self._observe_build(installation_in_progress)

    def on_realize(self, widget):
        self.wizard_view.content_navigation_view = self.content_navigation_view
        self.wizard_view._window = self._window
        self.wizard_view.set_installation(self.installation_in_progress)
        self.location_row.set_subtitle(GLib.markup_escape_text(project_builds_directory(self.project_directory)))
        toolset = self.toolset_selection_view.selected_item
        self._update_runs_on_row(toolset, self._selected_runner())
        self.load_stages()
        self._refresh_stage_requirements_async()
        self.update_plan()

    def _update_runs_on_row(self, toolset, runner):
        runner_name = runner.name if runner else "No runner"
        self.runs_on_row.set_subtitle(GLib.markup_escape_text(
            f"{runner_name} (toolset {toolset.name})" if toolset else runner_name))
        self.runs_on_row.set_visible(True)

    # Loading stages
    # --------------------------------------------------------------------------

    def load_stages(self):
        for stage, depth in self._stages_in_tree_order():
            row = Adw.ActionRow(title=GLib.markup_escape_text(stage.name))
            if getattr(stage, "is_broken", False):
                row.set_subtitle(GLib.markup_escape_text(stage.warning_message))
                row.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
                row.add_css_class("warning")
                self.stages_group.add(row)
                continue
            check_button = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            check_button.connect("toggled", self.on_stage_toggled, stage)
            row.add_prefix(check_button)
            row.add_prefix(Gtk.Image.new_from_icon_name(stage_target_icon(stage.target)))
            row.set_activatable_widget(check_button)
            warning_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            warning_icon.add_css_class("warning")
            warning_icon.set_visible(False)
            warning_icon.set_tooltip_text("Stage has missing required settings")
            row.add_suffix(warning_icon)
            row.stage = stage
            row.check_button = check_button
            row.warning_icon = warning_icon
            self.stages_group.add(row)
            self.rows[stage.id] = (row, check_button)
        if not self.rows:
            self.stages_group.add(Adw.ActionRow(title="This project has no stages yet"))

    def _stages_in_tree_order(self) -> list[tuple]:
        return [(node.value, depth) for node, depth in walk_tree(self.project_directory.stages_tree())]

    # Snapshot
    # --------------------------------------------------------------------------

    def load_snapshots(self):
        """Latest snapshot option and all snapshots (newest first), snapshot of project is selected."""
        self.snapshot_selection_view.selected_item = self._project_snapshot_option()
        self.snapshot_selection_view.set_static_list([LATEST_SNAPSHOT] + Repository.Snapshot.value)
        self._update_snapshot_row()

    def _toolset_changed(self, *_):
        self._refresh_stage_requirements_async()
        self._update_runs_on_row(self._selected_toolset(), self._selected_runner())
        self.snapshot_selection_view.refresh_items_state()
        self._update_snapshot_row()
        self.wizard_view._refresh_buttons_state()

    def _refresh_stage_requirements_async(self):
        """Check required spec values off the UI thread for the currently selected toolset."""
        if self._requirement_cancel is not None:
            self._requirement_cancel.set()
        if self._requirement_future is not None:
            self._requirement_future.cancel()
        self._requirement_generation += 1
        generation = self._requirement_generation
        cancelled = self._requirement_cancel = threading.Event()
        toolset = self._selected_toolset()
        project = self.project_directory
        stages = list(project.stages)
        self._requirement_statuses.clear()
        for stage_id, (row, _check) in self.rows.items():
            if getattr(row.stage, "is_broken", False):
                continue
            row.warning_icon.set_visible(False)
            plan_entry = getattr(self, "plan", None)
            build_entry = plan_entry.entries.get(stage_id) if plan_entry else None
            description = self._entry_description(build_entry) if build_entry else (
                getattr(row.stage, "target", None) or ""
            ).replace("_", "-")
            row.set_subtitle(GLib.markup_escape_text(f"{description} · Checking required settings…"))
            row.set_tooltip_text("Checking required settings…")
            if not (build_entry and build_entry.mode == StageBuildMode.REQUIRED):
                row.remove_css_class("warning")
        if getattr(self, "plan", None):
            self._update_summary()
        if toolset is None:
            for stage in stages:
                self._apply_stage_requirement_status(generation, stage.id, None)
            return

        def work():
            try:
                from .project_stage import refresh_default_stage_arguments
                evaluation_stages = []
                for original in stages:
                    stage = copy.copy(original)
                    stage.__dict__ = dict(original.__dict__)
                    for name in ("_default_stage_arguments", "_explicit_stage_arguments",
                                 "_explicit_none_arguments", "_explicit_user_arguments",
                                 "_explicit_user_argument_dates"):
                        value = getattr(stage, name, None)
                        if isinstance(value, dict):
                            setattr(stage, name, dict(value))
                        elif isinstance(value, set):
                            setattr(stage, name, set(value))
                    evaluation_stages.append(stage)
                evaluation_project = copy.copy(project)
                evaluation_project.stages = evaluation_stages
                snapshot = project.get_snapshot()
                context = StageSpecContext(
                    timestamp="<timestamp>", source_subpath="<stage seed>",
                    portage_confdir="<generated portage configuration>",
                    toolset=toolset,
                    cache_paths={StageArgumentDetails.pkgcache_path: "<package cache>",
                                 StageArgumentDetails.kerncache_path: "<kernel cache>"},
                    root_overlay="<generated root overlay>", iso_overlay="<generated ISO overlay>",
                    snapshot=snapshot,
                )
                for node, _depth in walk_tree(evaluation_project.stages_tree()):
                    if cancelled.is_set():
                        return
                    stage = node.value
                    try:
                        refresh_default_stage_arguments(evaluation_project, stage, toolset=toolset, snapshot=snapshot)
                        missing = missing_required_stage_values(evaluation_project, stage, context)
                        status = (StageNodeStatus(
                            title=f"Missing required: {', '.join(missing)}",
                            icon_name="dialog-warning-symbolic", css_class="warning",
                        ) if missing else None)
                    except Exception:
                        # Metadata failures are not evidence that a stage is misconfigured.
                        status = None
                    if cancelled.is_set():
                        return
                    GLib.idle_add(self._apply_stage_requirement_status, generation, stage.id, status)
            except Exception as error:
                print(f"Failed to check build stage requirements: {error}")

        self._requirement_future = self._requirement_executor.submit(work)

    def _apply_stage_requirement_status(self, generation, stage_id, status):
        if generation != self._requirement_generation:
            return GLib.SOURCE_REMOVE
        entry = self.rows.get(stage_id)
        if entry is None:
            return GLib.SOURCE_REMOVE
        row, _check = entry
        if getattr(row.stage, "is_broken", False):
            return GLib.SOURCE_REMOVE
        self._requirement_statuses[stage_id] = status
        plan_entry = getattr(self, "plan", None)
        build_entry = plan_entry.entries.get(stage_id) if plan_entry else None
        description = self._entry_description(build_entry) if build_entry else (
            getattr(row.stage, "target", None) or ""
        ).replace("_", "-")
        if status is not None:
            description += f" · {status.title}"
        row.set_subtitle(GLib.markup_escape_text(description))
        row.warning_icon.set_visible(status is not None)
        row.warning_icon.set_tooltip_text(status.title if status else None)
        row.set_tooltip_text(None)
        if status is not None or (build_entry and build_entry.mode == StageBuildMode.REQUIRED):
            row.add_css_class("warning")
        else:
            row.remove_css_class("warning")
        self._update_summary()
        self.wizard_view._refresh_buttons_state()
        return GLib.SOURCE_REMOVE

    def do_dispose(self):
        if self._requirement_cancel is not None:
            self._requirement_cancel.set()
        if self._requirement_future is not None:
            self._requirement_future.cancel()
        self._requirement_executor.shutdown(wait=False, cancel_futures=True)
        if self._preview_cancel is not None:
            self._preview_cancel.set()
        self._preview_generation += 1
        if self._preview_future is not None:
            self._preview_future.cancel()
        self._preview_executor.shutdown(wait=False, cancel_futures=True)
        Gtk.Widget.do_dispose(self)

    def _runner_changed(self, *_):
        self._update_runs_on_row(self._selected_toolset(), self._selected_runner())
        self.wizard_view._refresh_buttons_state()

    def _selected_toolset(self):
        return self.toolset_selection_view.selected_item

    def _selected_runner(self):
        return self.runner_selection_view.selected_item

    @Gtk.Template.Callback()
    def is_toolset_selectable(self, sender, item) -> bool:
        return toolset_has_applications(item, self.required_toolset_apps)

    @Gtk.Template.Callback()
    def is_toolset_usable(self, sender, item) -> bool:
        return item.busy_reason is None

    @Gtk.Template.Callback()
    def setup_toolset_monitoring(self, sender, items):
        for toolset in self._monitored_toolsets:
            toolset.event_bus.unsubscribe(SharedEvent.STATE_UPDATED, self)
        self._monitored_toolsets = items
        for toolset in items:
            toolset.event_bus.subscribe(SharedEvent.STATE_UPDATED, self._toolset_state_changed, self)

    def _toolset_state_changed(self, *_args):
        self.toolset_selection_view.refresh_items_state()
        selected = self._selected_toolset()
        if selected is None or selected.busy_reason is None:
            return
        for toolset in self._toolset_preference_order():
            if (toolset_has_applications(toolset, self.required_toolset_apps)
                    and toolset.busy_reason is None):
                self.toolset_selection_view.select(toolset)
                return

    def _toolset_preference_order(self):
        from .project_toolsets import get as get_project_toolset
        preferred_id = get_project_toolset(self.project_directory.directory_path())
        items = self.toolset_selection_view.items()
        preferred = next((item for item in items if str(item.uuid) == str(preferred_id)), None)
        default = default_toolset(applications=self.required_toolset_apps, toolsets=items)
        return list(dict.fromkeys(item for item in (preferred, default, *items) if item is not None))

    def snapshot_changed(self, view):
        self._update_snapshot_row()
        self.wizard_view._refresh_buttons_state()

    def _update_snapshot_row(self):
        selected = self.snapshot_selection_view.selected_item
        if selected is LATEST_SNAPSHOT:
            subtitle = "Latest, generated with the selected toolset before building"
        elif selected:
            subtitle = f"{selected.name} ({selected.short_details})"
        else:
            subtitle = "Not selected"
        if selected is not None and selected is self._project_snapshot_option():
            subtitle += ", snapshot of the project"
        self.snapshot_row.set_subtitle(GLib.markup_escape_text(subtitle))

    def _project_snapshot_option(self):
        """Snapshot option set in project: latest or its snapshot."""
        return LATEST_SNAPSHOT if self.project_directory.uses_latest_snapshot else self.project_directory.get_snapshot()

    def _toolset_generates_snapshots(self) -> bool:
        toolset = self._selected_toolset()
        return toolset is not None and toolset_has_applications(toolset, [ToolsetApplication.CATALYST])

    @Gtk.Template.Callback()
    def is_item_selectable(self, sender, item) -> bool:
        return item is not LATEST_SNAPSHOT or self._toolset_generates_snapshots()

    @Gtk.Template.Callback()
    def is_item_usable(self, sender, item) -> bool:
        return True

    # Build plan
    # --------------------------------------------------------------------------

    def update_plan(self):
        self.plan = StageBuildPlan(self.project_directory, self.selected_stage_ids, builds=self.builds)
        self._updating = True
        for stage_id, (row, check_button) in self.rows.items():
            entry = self.plan.entries[stage_id]
            check_button.set_active(entry.is_built)
            # Stages required by selected ones can't be unchecked, they are needed as seed.
            check_button.set_sensitive(entry.mode != StageBuildMode.REQUIRED)
            warning_status = self._requirement_statuses.get(stage_id)
            description = self._entry_description(entry)
            if warning_status is not None:
                description += f" · {warning_status.title}"
            row.set_subtitle(GLib.markup_escape_text(description))
            row.warning_icon.set_visible(warning_status is not None)
            row.warning_icon.set_tooltip_text(warning_status.title if warning_status else None)
            if entry.mode == StageBuildMode.REQUIRED or warning_status is not None:
                row.add_css_class("warning")
            else:
                row.remove_css_class("warning")
        self._updating = False
        self._update_summary()
        self.wizard_view._refresh_buttons_state()

    def _update_summary(self):
        for group, row in self.summary_rows:
            group.remove(row)
        self.summary_rows = []
        def add(group, title: str, subtitle: str, icon: str, warning_status=None):
            row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(subtitle), icon_name=icon)
            if warning_status is not None:
                row.add_css_class("warning")
                warning = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                warning.add_css_class("warning")
                warning.set_tooltip_text(warning_status.title)
                row.add_suffix(warning)
            group.add(row)
            self.summary_rows.append((group, row))
        for index, stage in enumerate(self.plan.build_order(), start=1):
            entry = self.plan.entries[stage.id]
            warning_status = self._requirement_statuses.get(stage.id)
            description = self._entry_description(entry)
            if warning_status is not None:
                description += f" · {warning_status.title}"
            add(self.build_order_group, f"{index}. {stage.name}", description,
                stage_target_icon(stage.target), warning_status)
        reused = [entry for entry in self.plan.entries.values() if entry.mode == StageBuildMode.REUSE]
        for entry in reused:
            add(self.reused_group, entry.stage.name, f"Build from {_format_date(entry.reused_build.date)}", stage_target_icon(entry.stage.target))
        self.reused_group.set_visible(bool(reused))

    def _entry_description(self, entry) -> str:
        names = ", ".join(stage.name for stage in entry.required_by)
        match entry.mode:
            case StageBuildMode.BUILD:
                return "Will be built"
            case StageBuildMode.REQUIRED:
                return f"Will be built, required by {names} ({self._no_build_reason(entry.stage)})"
            case StageBuildMode.REUSE:
                return f"Reusing build from {_format_date(entry.reused_build.date)} for {names}. Select to build again."
            case _:
                latest = self.plan.latest_build(entry.stage)
                return f"Last build: {_format_date(latest.date)}" if latest else self._no_build_reason(entry.stage).capitalize()

    def _no_build_reason(self, stage) -> str:
        attempt = self.plan.latest_attempt(stage)
        if attempt is None:
            return "not built yet"
        return f"last build {'failed' if attempt.status.value == 'failed' else 'not finished'}: {_format_date(attempt.date)}"

    # Handle UI
    # --------------------------------------------------------------------------

    def on_stage_toggled(self, check_button: Gtk.CheckButton, stage):
        if self._updating:
            return
        if check_button.get_active():
            self.selected_stage_ids.add(stage.id)
        else:
            self.selected_stage_ids.discard(stage.id)
        self.update_plan()

    @Gtk.Template.Callback()
    def on_select_all_clicked(self, button):
        self.selected_stage_ids = set(self.rows.keys())
        self.update_plan()

    @Gtk.Template.Callback()
    def on_clear_clicked(self, button):
        self.selected_stage_ids = set()
        self.update_plan()

    @Gtk.Template.Callback()
    def is_page_ready_to_continue(self, sender, page) -> bool:
        match page:
            case self.toolset_page:
                toolset = self._selected_toolset()
                runner = self._selected_runner()
                return (toolset is not None and self.is_toolset_selectable(self.toolset_selection_view, toolset)
                        and self.is_toolset_usable(self.toolset_selection_view, toolset)
                        and runner is not None and self.is_runner_usable(self.runner_selection_view, runner))
            case self.stages_page | self.summary_page:
                return bool(getattr(self, "plan", None) and self.plan.build_order())
            case self.snapshot_page:
                selected = self.snapshot_selection_view.selected_item
                return selected is not None and self.is_item_selectable(self.snapshot_selection_view, selected)
            case self.generated_files_page:
                return (self._preview_ready and self._preview_root is not None
                        and bool(getattr(self, "plan", None) and self.plan.build_order()))
        return True

    def _wizard_page_changed(self, carousel, _pspec):
        page = carousel.get_nth_page(int(carousel.get_position()))
        if page is self.generated_files_page:
            self._generate_build_file_preview()

    def _intercept_preview_back(self, wizard) -> bool:
        if wizard.current_page >= len(wizard.pages) or wizard.pages[wizard.current_page] is not self.generated_files_page:
            return False
        if self._preview_back_confirmed:
            self._preview_back_confirmed = False
            return False
        dialog = Adw.AlertDialog(
            heading="Return to build setup?",
            body="If you change any build options, the generated files will be regenerated when you return to this review. Any custom edits made directly to these files may be lost.",
        )
        dialog.add_response("stay", "Stay")
        dialog.add_response("back", "Continue")
        dialog.set_default_response("stay")
        dialog.set_close_response("stay")
        def on_response(_dialog, response):
            if response == "back":
                self._preview_back_confirmed = True
                wizard.on_back_pressed(None)
        dialog.connect("response", on_response)
        dialog.present(self.get_root())
        return True

    def _generate_build_file_preview(self):
        """Build preview files in a worker and replace stale output immediately."""
        from datetime import datetime, timezone

        if self._preview_cancel is not None:
            self._preview_cancel.set()
        if self._preview_future is not None:
            self._preview_future.cancel()
        self._preview_generation += 1
        generation = self._preview_generation
        self._preview_ready = False
        cancelled = self._preview_cancel = threading.Event()
        previous_root = self._preview_root
        self._preview_root = None
        self._clear_preview_rows()
        self.generated_files_info_group.set_visible(False)
        self.generated_files_location_row.set_visible(False)
        self.generated_files_location_row.set_title("Preview location")
        self.generated_files_location_row.set_subtitle("Preparing preview…")
        loading_row = Adw.ActionRow(title="Generating build files", subtitle="Checking each planned stage…")
        spinner = Adw.Spinner()
        spinner.set_visible(True)
        loading_row.add_suffix(spinner)
        self.generated_files_group.add(loading_row)
        self._preview_rows.append(loading_row)
        self.wizard_view._refresh_buttons_state()

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        plan = self.plan
        toolset = self._selected_toolset()
        runner = self._selected_runner()
        selected_snapshot = self.snapshot_selection_view.selected_item
        defaults_snapshot = (
            self.project_directory.get_snapshot()
            if selected_snapshot is LATEST_SNAPSHOT or selected_snapshot is None
            else selected_snapshot
        )
        self._preview_defaults_snapshot = defaults_snapshot
        cleanup_previous_builds = self.cleanup_previous_builds_row.get_active()

        def work():
            try:
                result = self._build_preview_files(
                    plan, toolset, runner, selected_snapshot, defaults_snapshot, cleanup_previous_builds, timestamp,
                    previous_root, cancelled,
                )
            except Exception as error:
                result = (None, [{"title": "Couldn't generate build files", "subtitle": str(error),
                                  "path": None, "error": True}], False)
            if not cancelled.is_set():
                GLib.idle_add(self._show_build_file_preview, generation, timestamp, result)

        self._preview_future = self._preview_executor.submit(work)

    def _build_preview_files(self, plan, toolset, runner, selected_snapshot, defaults_snapshot,
                             cleanup_previous_builds, timestamp, previous_root, cancelled):
        """Write preview files and return row descriptions without accessing GTK."""
        from .project_build_process import ProjectBuild, CATALYST_WRAPPER, _catalyst_build_script, _catalyst_cache_options
        from .project_build_spec import (
            generate_portage_confdir, generate_root_overlay, StageSpecContext, _stage_spec_lines,
        )
        from .project_stage_arguments import StageArgumentDetails

        preview_root = None
        rows = []
        latest_snapshot = selected_snapshot is LATEST_SNAPSHOT
        try:
            if previous_root:
                shutil.rmtree(previous_root, ignore_errors=True)
            preview_parent = resolve_path(Repository.Settings.value.temporary_location)
            os.makedirs(preview_parent, exist_ok=True)
            preview_root = tempfile.mkdtemp(prefix="catalyst-build-preview-", dir=preview_parent)
            snapshot = defaults_snapshot if latest_snapshot else selected_snapshot
            process = ProjectBuild(
                self.project_directory, plan, toolset, runner, snapshot=snapshot, fetch_snapshot=False,
                cleanup_previous_builds=cleanup_previous_builds, timestamp=timestamp,
                defaults_snapshot=defaults_snapshot,
            )
            process.work_root = preview_root
            rosetta_amd64 = False
            rosetta_detection_error = None
            architecture = self.project_directory.get_architecture()
            if getattr(architecture, "value", None) == "amd64":
                try:
                    rosetta_amd64 = process.rosetta_amd64_available()
                except Exception as error:
                    rosetta_detection_error = str(error)
            evaluation_stages = []
            for original in self.project_directory.stages:
                stage_copy = copy.copy(original)
                stage_copy.__dict__ = dict(original.__dict__)
                for name in ("_default_stage_arguments", "_explicit_stage_arguments",
                             "_explicit_none_arguments", "_explicit_user_arguments",
                             "_explicit_user_argument_dates"):
                    value = getattr(stage_copy, name, None)
                    if isinstance(value, dict):
                        setattr(stage_copy, name, dict(value))
                    elif isinstance(value, set):
                        setattr(stage_copy, name, set(value))
                evaluation_stages.append(stage_copy)
            evaluation_project = copy.copy(self.project_directory)
            evaluation_project.stages = evaluation_stages
            evaluation_stages_by_id = {stage.id: stage for stage in evaluation_stages}
            for stage in plan.build_order():
                if cancelled.is_set():
                    shutil.rmtree(preview_root, ignore_errors=True)
                    return None, [], latest_snapshot
                work_directory = process.stage_work_directory(stage)
                os.makedirs(work_directory, exist_ok=True)
                portage_path = os.path.join(work_directory, "portage")
                has_confdir = generate_portage_confdir(self.project_directory, stage, portage_path)
                root_overlay_path = os.path.join(work_directory, "root_overlay")
                has_root_overlay = generate_root_overlay(self.project_directory, stage, root_overlay_path)
                iso_overlay_path = os.path.join(work_directory, "iso_overlay")
                has_iso_overlay = generate_root_overlay(
                    self.project_directory, stage, iso_overlay_path, StageArgumentDetails.livecd_overlay,
                )
                source_subpath = process.seed_subpath(stage)
                runtime_inputs = []
                if source_subpath is None:
                    source_subpath = "<source_subpath>"
                    runtime_inputs.append("source seed path")
                cache_paths = process.stage_cache_paths(stage)
                try:
                    spec_lines, missing = _stage_spec_lines(
                        evaluation_project, evaluation_stages_by_id[stage.id], StageSpecContext(
                            timestamp=timestamp,
                            source_subpath=source_subpath,
                            portage_confdir=process.container_path(portage_path) if has_confdir else None,
                            toolset=process.toolset,
                            root_overlay=process.container_path(root_overlay_path) if has_root_overlay else None,
                            iso_overlay=process.container_path(iso_overlay_path) if has_iso_overlay else None,
                            cache_paths={argument: process.container_path(path) for argument, path in cache_paths.items()},
                            snapshot=snapshot,
                            path_mapper=process.container_path,
                            rosetta_amd64=rosetta_amd64,
                            defaults_snapshot=defaults_snapshot,
                        ),
                    )
                    spec = "\n".join(spec_lines) + "\n"
                    if latest_snapshot:
                        spec_lines = spec.splitlines()
                        for index, line in enumerate(spec_lines):
                            if line.startswith("snapshot_treeish:"):
                                spec_lines[index] = "snapshot_treeish: <snapshot_latest>"
                                runtime_inputs.append("snapshot treeish")
                        spec = "\n".join(spec_lines) + "\n"
                except Exception as error:
                    with open(os.path.join(work_directory, "PREVIEW-ERROR.txt"), "w", encoding="utf-8") as file:
                        file.write(f"Couldn't generate stage.spec: {error}\n")
                    rows.append({"title": stage.name, "subtitle": f"Spec generation failed: {error}",
                                 "path": work_directory, "error": True})
                    continue
                with open(os.path.join(work_directory, "stage.spec"), "w", encoding="utf-8") as file:
                    file.write(spec)
                wrapper_path = None
                if process.rootless:
                    wrapper_path = os.path.join(work_directory, "catalyst-wrapper.py")
                    with open(wrapper_path, "w", encoding="utf-8") as file:
                        file.write(CATALYST_WRAPPER)
                build_script_path = os.path.join(work_directory, "build.sh")
                with open(build_script_path, "w", encoding="utf-8") as file:
                    file.write(_catalyst_build_script(
                        spec_path=process.container_path(os.path.join(work_directory, "stage.spec")),
                        config_path=process.container_path(os.path.join(work_directory, "catalyst.conf")),
                        caches={_catalyst_cache_options[argument] for argument in cache_paths},
                        wrapper_path=process.container_path(wrapper_path) if wrapper_path else None,
                    ))
                for overlay_path, present in ((portage_path, has_confdir), (root_overlay_path, has_root_overlay),
                                              (iso_overlay_path, has_iso_overlay)):
                    if present and not os.path.isdir(overlay_path):
                        os.makedirs(overlay_path, exist_ok=True)
                files = ["stage.spec", "build.sh"]
                if wrapper_path:
                    files.append("catalyst-wrapper.py")
                files.extend(name for name, present in (
                    ("portage/", has_confdir), ("root_overlay/", has_root_overlay),
                    ("iso_overlay/", has_iso_overlay),
                ) if present)
                with open(os.path.join(work_directory, "PREVIEW-NOTES.txt"), "w", encoding="utf-8") as file:
                    file.write("These are the files used by the build. Edit them before starting the build if needed.\n")
                    if "source seed path" in runtime_inputs:
                        file.write("<source_subpath> is filled with the seed selected or produced when the build runs, unless you replace it.\n")
                    if latest_snapshot:
                        file.write("The build uses a newly generated latest snapshot. <snapshot_latest> is resolved at build time; snapshot-dependent defaults keep the values established in this wizard.\n")
                    file.write("catalyst.conf is generated inside the selected toolset when build.sh runs.\n")
                    if rosetta_detection_error:
                        file.write(f"Rosetta detection failed during preview ({rosetta_detection_error}); the build checks again after preparing the runner.\n")
                    elif rosetta_amd64:
                        file.write("Rosetta is active for amd64; Catalyst interpreter is omitted.\n")
                    elif getattr(architecture, "value", None) == "amd64":
                        file.write("Rosetta is not active for amd64; the Catalyst interpreter follows the project architecture.\n")
                if missing:
                    subtitle = f"Missing required: {', '.join(missing)}"
                else:
                    subtitle = " · ".join(files)
                    if "source seed path" in runtime_inputs:
                        subtitle += " · seed path resolved when the build starts"
                    if "snapshot treeish" in runtime_inputs:
                        subtitle += " · snapshot treeish resolved when the build starts"
                rows.append({"title": stage.name, "subtitle": subtitle, "path": work_directory,
                             "error": False, "warning": bool(missing)})
            if cancelled.is_set():
                shutil.rmtree(preview_root, ignore_errors=True)
                return None, [], latest_snapshot
            return preview_root, rows, latest_snapshot
        except Exception as error:
            if cancelled.is_set() and preview_root:
                shutil.rmtree(preview_root, ignore_errors=True)
                return None, [], latest_snapshot
            rows.append({"title": "Couldn't generate build files", "subtitle": str(error),
                         "path": None, "error": True})
            return preview_root, rows, latest_snapshot

    def _show_build_file_preview(self, generation, timestamp, result):
        if generation != self._preview_generation:
            return GLib.SOURCE_REMOVE
        preview_root, rows, latest_snapshot = result
        self._clear_preview_rows()
        self._preview_root = preview_root
        self._preview_timestamp = timestamp
        for row_data in rows:
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(row_data["title"]),
                subtitle=GLib.markup_escape_text(row_data["subtitle"]),
            )
            if row_data.get("warning"):
                row.add_css_class("warning")
                warning_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                warning_icon.add_css_class("warning")
                warning_icon.set_tooltip_text(row_data["subtitle"])
                row.add_prefix(warning_icon)
            elif row_data.get("error"):
                row.add_css_class("error")
            if row_data["path"]:
                open_button = Gtk.Button(label="Browse")
                open_button.set_valign(Gtk.Align.CENTER)
                open_button.connect("clicked", self._open_preview_directory, row_data["path"])
                row.add_suffix(open_button)
            self.generated_files_group.add(row)
            self._preview_rows.append(row)
        if preview_root:
            self.generated_files_location_row.set_subtitle(GLib.markup_escape_text(preview_root))
            self.generated_files_location_row.set_visible(True)
            self.generated_files_info_group.set_visible(True)
            if latest_snapshot:
                self.generated_files_location_row.set_title(
                    "Preview files (latest snapshot may update defaults at build time)"
                )
        self._preview_ready = bool(preview_root) and all(
            row_data["path"] and os.path.isfile(os.path.join(row_data["path"], "stage.spec"))
            for row_data in rows
        )
        self.wizard_view._refresh_buttons_state()
        return GLib.SOURCE_REMOVE

    def _clear_preview_rows(self):
        for row in self._preview_rows:
            self.generated_files_group.remove(row)
        self._preview_rows = []

    def _open_preview_directory(self, _button, path):
        try:
            Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(path), None)
        except GLib.Error as error:
            dialog = Adw.AlertDialog(heading="Couldn't open generated files", body=str(error))
            dialog.add_response("ok", "OK")
            dialog.present(self.get_root())

    @Gtk.Template.Callback()
    def on_browse_all_generated_files(self, _button):
        if self._preview_root and os.path.isdir(self._preview_root):
            self._open_preview_directory(_button, self._preview_root)

    @staticmethod
    def _queue_mode() -> bool:
        return running_build() is not None

    @Gtk.Template.Callback()
    def begin_installation(self, view):
        if not self._preview_ready or self._preview_root is None:
            return
        if self._queue_mode():
            # Toolset can be used by running build, it's checked when queued build starts.
            toolset = self._selected_toolset()
            runner = self._selected_runner()
            if toolset is None or runner is None:
                return
            if reason := BuildQueue.shared().queue_unavailable_reason(self.project_directory):
                dialog = Adw.AlertDialog(heading="Can't add build to queue", body=reason)
                dialog.add_response("ok", "OK")
                dialog.present(self.get_root())
                return
            self._choose_project_snapshot()
            return
        toolset = self._selected_toolset()
        runner = self._selected_runner()
        # Toolset or its virtual machine could become busy while wizard was open.
        if busy_reason := (toolset.busy_reason if toolset else "Project has no toolset"):
            dialog = Adw.AlertDialog(heading="Can't start build", body=busy_reason)
            dialog.add_response("ok", "OK")
            dialog.present(self.get_root())
            return
        if runner is None or not runner_is_supported(runner):
            dialog = Adw.AlertDialog(heading="Can't start build", body="Select an available runner")
            dialog.add_response("ok", "OK")
            dialog.present(self.get_root())
            return
        self._choose_project_snapshot()

    def _choose_project_snapshot(self):
        selected = self.snapshot_selection_view.selected_item
        # Snapshot option of project doesn't change project settings.
        if selected is not self._project_snapshot_option():
            self._ask_update_project_snapshot(selected)
        else:
            self._authorize_and_start(update_project_snapshot=False)

    def _ask_update_project_snapshot(self, selected):
        """Snapshot different than project snapshot is used in this build, it can be stored in project too."""
        if selected is LATEST_SNAPSHOT:
            body = ("The latest snapshot will be generated before building. Do you want the project to always get the "
                    "latest snapshot when building?")
        else:
            body = f"This build uses snapshot {selected.name}. Do you want the project to use it as well?"
        dialog = Adw.AlertDialog(heading="Update project snapshot?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("build", "Only this build")
        dialog.add_response("project", "Update project")
        dialog.set_response_appearance("project", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("project")
        dialog.set_close_response("cancel")
        def on_response(dialog, response):
            if response != "cancel":
                self._authorize_and_start(update_project_snapshot=response == "project")
        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    def _authorize_and_start(self, update_project_snapshot: bool):
        self.update_project_snapshot = update_project_snapshot
        selected = self.snapshot_selection_view.selected_item
        fetch = selected is LATEST_SNAPSHOT
        if fetch and update_project_snapshot:
            # Project gets latest snapshot in next builds too, newest snapshot is its snapshot.
            metadata = self.project_directory.initialize_metadata()
            metadata.latest_snapshot = True
            Repository.ProjectDirectory.save()
        if self._queue_mode():
            self._add_to_queue(selected, fetch, update_project_snapshot)
            return
        start_project_build(self.project_directory, self.plan, toolset=self._selected_toolset(), runner=self._selected_runner(), snapshot=None if fetch else selected, fetch_snapshot=fetch,
                            update_project_snapshot=update_project_snapshot and not fetch,
                            cleanup_previous_builds=self.cleanup_previous_builds_row.get_active(),
                            timestamp=self._preview_timestamp,
                            defaults_snapshot=self._preview_defaults_snapshot,
                            preview_root=self._preview_root,
                            done=self._on_build_started)

    def _add_to_queue(self, selected, fetch: bool, update_project_snapshot: bool):
        if update_project_snapshot and not fetch:
            # Snapshot is project snapshot from now, like when build starts.
            metadata = self.project_directory.initialize_metadata()
            metadata.snapshot_id = selected.filename
            metadata.latest_snapshot = False
            Repository.ProjectDirectory.save()
        try:
            BuildQueue.shared().add(self.project_directory, list(self.selected_stage_ids), LATEST if fetch else selected.filename,
                                    cleanup_previous_builds=self.cleanup_previous_builds_row.get_active(),
                                    toolset=self._selected_toolset(), runner=self._selected_runner(),
                                    defaults_snapshot=self._preview_defaults_snapshot,
                                    preview_root=self._preview_root, timestamp=self._preview_timestamp)
        except RuntimeError as e:
            dialog = Adw.AlertDialog(heading="Can't add build to queue", body=str(e))
            dialog.add_response("ok", "OK")
            dialog.present(self.get_root())
            return
        # Wizard shows queued build (steps and waiting in queue), and its progress when it starts.
        from .build_queue_view import QueuedBuildView
        queued_view = QueuedBuildView(BuildQueue.shared().entry_for(self.project_directory))
        queued_view._window = self._window
        queued_view.set_vexpand(True)
        self.remove(self.wizard_view)
        self.append(queued_view)

    def _on_build_started(self, build: ProjectBuild | None):
        if build is None:
            print("Failed to start build")
            return
        if self.update_project_snapshot and not build.fetch_snapshot:
            build.store_project_snapshot()
        self.wizard_view.set_installation(build)
        self._observe_build(build)

    def _observe_build(self, build: ProjectBuild | None):
        if build is None or build is self._observed_build:
            return
        self._observed_build = build
        build.event_bus.subscribe(MultiStageProcessEvent.STATE_CHANGED, self._on_build_state_changed)

    def _on_build_state_changed(self, *args):
        self._mark_result_seen()

    def _mark_result_seen(self):
        build = self._observed_build
        if build is not None and self.get_mapped() and build.status in (MultiStageProcessState.COMPLETED, MultiStageProcessState.FAILED):
            self.project_directory.mark_build_result_seen()

    @Gtk.Template.Callback()
    def is_runner_selectable(self, _sender, _runner) -> bool:
        return True

    @Gtk.Template.Callback()
    def setup_runner_monitoring(self, sender, items):
        for runner in getattr(self, "monitored_runners", ()):
            runner.event_bus.unsubscribe(SharedEvent.STATE_UPDATED, self)
        self.monitored_runners = items
        for runner in items:
            runner.event_bus.subscribe(SharedEvent.STATE_UPDATED, sender.refresh_items_state, self)

    @Gtk.Template.Callback()
    def is_runner_usable(self, _sender, runner) -> bool:
        if not runner_is_supported(runner):
            return False
        if runner.runner_type == RunnerType.VM:
            return runner.status not in (runner.STATUS_STARTING, runner.STATUS_STOPPING, runner.STATUS_MISSING)
        return not getattr(runner, "_exclusive_user", None)

def _format_date(date) -> str:
    return date.strftime("%Y-%m-%d %H:%M")
