from __future__ import annotations
from gi.repository import Gtk, Adw, GLib
from .project_directory import ProjectDirectory
from .project_build import StageBuildPlan, StageBuildMode, load_project_builds, project_builds_directory
from .project_build_process import ProjectBuild, start_project_build
from .build_queue import BuildQueue, running_build, LATEST
from .project_stage import stage_target_icon
from .lima import virtual_machines_supported
from .multistage_process import MultiStageProcessState, MultiStageProcessEvent
from .wizard_view import WizardView
from .repository import Repository
from .item_select_view import ItemSelectionViewEvent
from .toolset_application import ToolsetApplication
from .snapshot import LATEST_SNAPSHOT

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/project/project_build_view.ui')
class ProjectBuildView(Gtk.Box):
    """Wizard for building project stages. Parents of selected stages are reused from previous builds when possible,
    otherwise they are built too."""
    __gtype_name__ = "ProjectBuildView"

    # Main views:
    wizard_view = Gtk.Template.Child()
    # Setup view elements:
    stages_page = Gtk.Template.Child()
    snapshot_page = Gtk.Template.Child()
    snapshot_selection_view = Gtk.Template.Child()
    summary_page = Gtk.Template.Child()
    stages_group = Gtk.Template.Child()
    build_order_group = Gtk.Template.Child()
    reused_group = Gtk.Template.Child()
    location_row = Gtk.Template.Child()
    runs_on_row = Gtk.Template.Child()
    snapshot_row = Gtk.Template.Child()

    def __init__(self, project_directory: ProjectDirectory, installation_in_progress: ProjectBuild | None = None, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__()
        self.project_directory = project_directory
        self.installation_in_progress = installation_in_progress
        self.content_navigation_view = content_navigation_view
        self.selected_stage_ids: set = set()
        self.builds = load_project_builds(project_directory)
        self.rows: dict = {} # stage id -> (row, check_button)
        self.summary_rows: list = []
        self._updating = False
        self.snapshot_selection_view.event_bus.subscribe(ItemSelectionViewEvent.ITEM_CHANGED, self.snapshot_changed)
        self.load_snapshots()
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
        toolset = self.project_directory.get_toolset()
        self.runs_on_row.set_subtitle(GLib.markup_escape_text(
            f"{toolset.runs_on_name} (toolset {toolset.name})" if toolset else "No toolset"))
        self.runs_on_row.set_visible(virtual_machines_supported() or (toolset is not None and toolset.machine is not None))
        self.load_stages()
        self.update_plan()

    # Loading stages
    # --------------------------------------------------------------------------

    def load_stages(self):
        for stage, depth in self._stages_in_tree_order():
            row = Adw.ActionRow(title=GLib.markup_escape_text(stage.name))
            check_button = Gtk.CheckButton(valign=Gtk.Align.CENTER, margin_start=depth * 24)
            check_button.connect("toggled", self.on_stage_toggled, stage)
            row.add_prefix(check_button)
            row.add_prefix(Gtk.Image.new_from_icon_name(stage_target_icon(stage.target)))
            row.set_activatable_widget(check_button)
            self.stages_group.add(row)
            self.rows[stage.id] = (row, check_button)
        if not self.rows:
            self.stages_group.add(Adw.ActionRow(title="This project has no stages yet"))

    def _stages_in_tree_order(self) -> list[tuple]:
        result = []
        def visit(node, depth):
            result.append((node.value, depth))
            for child in node.children:
                visit(child, depth + 1)
        for root in self.project_directory.stages_tree():
            visit(root, 0)
        return result

    # Snapshot
    # --------------------------------------------------------------------------

    def load_snapshots(self):
        """Latest snapshot option and all snapshots (newest first), snapshot of project is selected."""
        self.snapshot_selection_view.selected_item = self._project_snapshot_option()
        self.snapshot_selection_view.set_static_list([LATEST_SNAPSHOT] + Repository.Snapshot.value)
        self._update_snapshot_row()

    def snapshot_changed(self, view):
        self._update_snapshot_row()
        self.wizard_view._refresh_buttons_state()

    def _update_snapshot_row(self):
        selected = self.snapshot_selection_view.selected_item
        if selected is LATEST_SNAPSHOT:
            subtitle = "Latest, generated with the project toolset before building"
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
        toolset = self.project_directory.get_toolset()
        return toolset is not None and toolset.get_app_install(ToolsetApplication.CATALYST) is not None

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
            row.set_subtitle(GLib.markup_escape_text(self._entry_description(entry)))
            if entry.mode == StageBuildMode.REQUIRED:
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
        def add(group, title: str, subtitle: str, icon: str):
            row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(subtitle), icon_name=icon)
            group.add(row)
            self.summary_rows.append((group, row))
        for index, stage in enumerate(self.plan.build_order(), start=1):
            entry = self.plan.entries[stage.id]
            add(self.build_order_group, f"{index}. {stage.name}", self._entry_description(entry), stage_target_icon(stage.target))
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
            case self.stages_page | self.summary_page:
                return bool(getattr(self, "plan", None) and self.plan.build_order())
            case self.snapshot_page:
                selected = self.snapshot_selection_view.selected_item
                return selected is not None and self.is_item_selectable(self.snapshot_selection_view, selected)
        return True

    @staticmethod
    def _queue_mode() -> bool:
        return running_build() is not None

    @Gtk.Template.Callback()
    def begin_installation(self, view):
        if self._queue_mode():
            # Toolset can be used by running build, it's checked when queued build starts.
            if reason := BuildQueue.shared().queue_unavailable_reason(self.project_directory):
                dialog = Adw.AlertDialog(heading="Can't add build to queue", body=reason)
                dialog.add_response("ok", "OK")
                dialog.present(self.get_root())
                return
            self._choose_project_snapshot()
            return
        toolset = self.project_directory.get_toolset()
        # Toolset or its virtual machine could become busy while wizard was open.
        if busy_reason := (toolset.busy_reason if toolset else "Project has no toolset"):
            dialog = Adw.AlertDialog(heading="Can't start build", body=busy_reason)
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
        start_project_build(self.project_directory, self.plan, snapshot=None if fetch else selected, fetch_snapshot=fetch,
                            update_project_snapshot=update_project_snapshot and not fetch, done=self._on_build_started)

    def _add_to_queue(self, selected, fetch: bool, update_project_snapshot: bool):
        if update_project_snapshot and not fetch:
            # Snapshot is project snapshot from now, like when build starts.
            metadata = self.project_directory.initialize_metadata()
            metadata.snapshot_id = selected.filename
            metadata.latest_snapshot = False
            Repository.ProjectDirectory.save()
        try:
            BuildQueue.shared().add(self.project_directory, list(self.selected_stage_ids), LATEST if fetch else selected.filename)
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

def _format_date(date) -> str:
    return date.strftime("%Y-%m-%d %H:%M")
