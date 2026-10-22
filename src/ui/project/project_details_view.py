from gi.repository import Gtk, Adw, GLib
import os
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from .git_directory import GitDirectoryEvent, GitDirectoryStatus
from .git_update import GitUpdate
from .project_manager import ProjectManager
from .project_directory import ProjectDirectory
from .event_bus import SharedEvent
from .repository import Repository
from .item_select_view import ItemSelectionViewEvent
from .project_stage_create_view import ProjectStageCreateView
from .app_events import app_event_bus, AppEvents
from .stages_tree_view import StagesTreeView, StagesListView, StageNodeStatus
from .project_stage_details_view import ProjectStageDetailsView
from .project_build_view import ProjectBuildView
from .build_queue_view import build_button_state, present_queued_build
from .build_queue import BuildQueue, BuildQueueEvent
from .project_build_process import ProjectBuild, ProjectBuildStepBuildStage, running_project_build
from .project_build_spec import StageSpecContext, missing_required_stage_values
from .project_stage_arguments import StageArgumentDetails
from .toolset_application import ToolsetApplication
from .project_builds_view import ProjectBuildsView
from .multistage_process import MultiStageProcess, MultiStageProcessState, MultiStageProcessEvent, MultiStageProcessStageEvent, MultiStageProcessStageState
from .architecture import Architecture
from .snapshot import LATEST_SNAPSHOT
from .cl_toggle_group import CLToggle, CLToggleGroup
from .project_template_update import (
    TemplateState, repository_origin, has_unsaved_changes, template_update_available, project_overlays
)
from .project_update_view import ProjectUpdateView
from .tree_node import walk_tree
import threading

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/project/project_details_view.ui')
class ProjectDetailsView(Gtk.Box):
    __gtype_name__ = "ProjectDetailsView"

    stack = Gtk.Template.Child()
    stage_content_stack = Gtk.Template.Child()
    stages_compact_view = Gtk.Template.Child()
    stages_full_view = Gtk.Template.Child()
    stages_list_view = Gtk.Template.Child()
    build_row = Gtk.Template.Child()
    build_row_label = Gtk.Template.Child()
    build_progress_row = Gtk.Template.Child()
    builds_row = Gtk.Template.Child()
    directory_details_view = Gtk.Template.Child()
    releng_selection_view = Gtk.Template.Child()
    snapshot_selection_view = Gtk.Template.Child()
    arch_selection_view = Gtk.Template.Child()
    packages_selection_view = Gtk.Template.Child()
    source_banner = Gtk.Template.Child()
    stage_actions = Gtk.Template.Child()
    source_action_row = Gtk.Template.Child()
    add_stage_row = Gtk.Template.Child()
    project_toolset_button = Gtk.Template.Child()
    project_toolset_button_label = Gtk.Template.Child()
    source_button = Gtk.Template.Child()
    source_button_label = Gtk.Template.Child()
    delete_project_button = Gtk.Template.Child()

    def __init__(self, project_directory: ProjectDirectory, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__()
        self.project_directory = project_directory
        self.content_navigation_view = content_navigation_view
        self._stages_refresh_pending = False
        if self.content_navigation_view is not None:
            self.content_navigation_view.connect("notify::visible-page", self._on_navigation_visible_page_changed)
        self._setup_action_button_styles()
        self._source = None
        self._build_action_available = False
        self._build_running = False
        self.directory_details_view.setup(git_directory=project_directory, content_navigation_view=self.content_navigation_view)
        # The Information page may not be realized when the footer delete action is used. Set the manager here instead
        # of relying on GitDirectoryDetailsView to resolve its manager lazily on realization, so project-specific
        # cleanup choices are always included in the confirmation dialog.
        self.directory_details_view.manager_class = ProjectManager
        self.directory_details_view.on_project_deleted = self._close_after_delete
        self.delete_project_button.connect(
            "clicked", self.directory_details_view.action_button_delete_clicked
        )
        self._setup_save_changes_button()
        self._update_project_toolset_button()
        self.get_configuration()
        self.monitor_stages_changes()
        self.monitor_information_changes()
        self.monitor_configuration_changes()
        stages = project_directory.stages_tree()
        self.stages_compact_view.set_root_nodes(stages)
        self.stages_full_view.set_root_nodes(stages)
        self.stages_list_view.set_root_nodes(stages)
        self._update_project_toolset_button()
        self._stage_requirement_statuses = {}
        self._stage_requirement_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="stage-requirements"
        )
        self._stage_requirement_cancel = None
        self._stage_requirement_future = None
        self._setup_stages_view_mode()
        self._setup_source_banner()
        # Result of last build is seen, project is not marked anymore in projects list.
        project_directory.mark_build_result_seen()
        self._observed_builds: set[int] = set() # Ids of builds whose changes are observed.
        self._build_refresh_scheduled = False
        MultiStageProcess.event_bus.subscribe(MultiStageProcessEvent.STARTED_PROCESSES_CHANGED, self._on_build_changed)
        BuildQueue.shared().event_bus.subscribe(BuildQueueEvent.CHANGED, self._on_build_changed)
        MultiStageProcess.event_bus.subscribe(
            MultiStageProcessEvent.STARTED_PROCESSES_CHANGED, self._update_action_rows_visibility
        )
        self._refresh_build_state()
        self._refresh_stage_requirement_statuses_async()

    def _setup_save_changes_button(self):
        """Add a compact save/discard split button to the shared project footer."""
        self.save_changes_button = Adw.SplitButton(
            child=Gtk.Label(label="Save changes"),
            valign=Gtk.Align.CENTER,
        )
        self.save_changes_button.add_css_class("project-footer-save-changes")
        self.save_changes_button.set_menu_model(self.directory_details_view.action_button_save_changes.get_menu_model())
        self.save_changes_button.connect(
            "clicked", self.directory_details_view.action_button_save_changes_clicked
        )
        # The Information tab's menu uses the same action names. Install its action group at the project page too,
        # so the footer menu invokes the exact same save/discard callbacks.
        self.insert_action_group("changes", self.directory_details_view._changes_action_group)
        self.save_changes_button.set_halign(Gtk.Align.START)
        self.source_action_row.insert_child_after(self.save_changes_button, self.project_toolset_button)

    def _setup_action_button_styles(self):
        """Apply action styles after template construction so every button gets the intended classes and sizing."""
        for button in (self.add_stage_row, self.builds_row, self.build_row, self.build_progress_row):
            button.add_css_class("project-action-button")
            button.set_size_request(-1, 36)
        self.add_stage_row.add_css_class("suggested-action")
        self.build_row.add_css_class("suggested-action")

        self.source_button.set_has_frame(False)
        for button in (self.project_toolset_button, self.source_button):
            for css_class in ("flat", "caption", "stage-toolset-change-button"):
                button.add_css_class(css_class)
        self.project_toolset_button_label.add_css_class("dim-label")
        self.project_toolset_button_label.add_css_class("caption")
        self.source_button_label.add_css_class("dim-label")
        self.source_button_label.add_css_class("caption")

    def _project_catalyst_toolsets(self):
        from .toolset_selection import sorted_toolsets, toolset_has_applications
        return [item for item in sorted_toolsets()
                if toolset_has_applications(item, [ToolsetApplication.CATALYST])
                and os.path.isfile(item.file_path())]

    def _update_project_toolset_button(self):
        from .project_toolsets import get
        from .toolset_selection import default_toolset
        toolsets = self._project_catalyst_toolsets()
        selected_id = get(self.project_directory.directory_path())
        selected = next((item for item in toolsets if str(item.uuid) == str(selected_id)), None)
        selected = selected or default_toolset([ToolsetApplication.CATALYST], toolsets) or (toolsets[0] if toolsets else None)
        label = f"Catalyst toolset · {selected.name}" if selected else "No Catalyst toolset available"
        self.project_toolset_button_label.set_label(label)
        self.project_toolset_button.set_tooltip_text("Catalyst toolset used for this project")
        self.project_toolset_button.set_sensitive(bool(toolsets))

    @Gtk.Template.Callback()
    def _on_change_project_toolset_clicked(self, _button):
        from .project_toolsets import get, set as set_project_toolset
        from .toolset_selection import default_toolset
        toolsets = self._project_catalyst_toolsets()
        if not toolsets:
            return
        current_id = get(self.project_directory.directory_path())
        default = default_toolset([ToolsetApplication.CATALYST], toolsets)
        dialog = Adw.Dialog(
            title="Catalyst toolset for project",
            content_width=420,
            presentation_mode=Adw.DialogPresentationMode.FLOATING,
        )
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_top=18, margin_bottom=12, margin_start=18, margin_end=18)
        dialog.set_child(content)
        content.append(Gtk.Label(
            label="All stages in this project use the selected Catalyst toolset.",
            wrap=True,
            xalign=0,
            css_classes=["dim-label"],
        ))
        scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      max_content_height=320, propagate_natural_height=True)
        list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                               css_classes=["boxed-list", "toolset-chooser"],
                               margin_top=4, margin_bottom=4, margin_start=4, margin_end=4)
        scroller.set_child(list_box)
        content.append(scroller)

        def choose_toolset(_list, row):
            set_project_toolset(self.project_directory.directory_path(), str(row.toolset.uuid))
            self._update_project_toolset_button()
            self._refresh_stage_requirement_statuses_async()
            dialog.close()

        list_box.connect("row-activated", choose_toolset)
        for toolset in toolsets:
            row = Adw.ActionRow(title=toolset.name,
                                subtitle=toolset.short_details or toolset.busy_reason or "Catalyst available",
                                activatable=True)
            row.add_prefix(Gtk.Image.new_from_icon_name("toolbox-symbolic"))
            if toolset is default:
                row.add_css_class("default-toolset-row")
            row.toolset = toolset
            list_box.append(row)
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        cancel = Gtk.Button(label="Cancel", hexpand=True)
        cancel.connect("clicked", lambda *_: dialog.close())
        actions.append(cancel)
        content.append(actions)
        dialog.present(self.get_root())

    def get_configuration(self):
        self.releng_selection_view.select(self.project_directory.get_releng_directory())
        self.snapshot_selection_view.set_leading_items([LATEST_SNAPSHOT])
        self.snapshot_selection_view.select(
            LATEST_SNAPSHOT if self.project_directory.uses_latest_snapshot else self.project_directory.get_snapshot()
        )
        self.arch_selection_view.select(self.project_directory.get_architecture())
        self.arch_selection_view.set_static_list(sorted(Architecture, key=lambda arch: arch.name))
        self.packages_selection_view.select(self.project_directory.get_packages_directory())
        self._update_packages_warning()
        if not getattr(self, "_new_packages_row", None):
            self._new_packages_row = Adw.ButtonRow(title="New folder for project", start_icon_name="add-square-svgrepo-com-symbolic")
            self._new_packages_row.connect("activated", self.on_new_packages_activated)
            self.packages_selection_view.add_footer_row(self._new_packages_row)

    def _update_packages_warning(self):
        """Selected binary packages folder made for other architecture or CPU flags than project has."""
        directory = self.project_directory.get_packages_directory()
        problems = directory.compatibility(self.project_directory) if directory else []
        self.packages_selection_view.set_extra_warning("\n".join(problem.text for problem in problems) or None)

    def configuration_item_changed(self, container):
        match container:
            case self.releng_selection_view:
                self.project_directory.initialize_metadata().releng_directory_id = (
                    self.releng_selection_view.selected_item.id
                    if self.releng_selection_view.selected_item else None
                )
            case self.snapshot_selection_view:
                selected = self.snapshot_selection_view.selected_item
                metadata = self.project_directory.initialize_metadata()
                # Latest: builds generate latest snapshot, previously selected snapshot is kept.
                metadata.latest_snapshot = selected is LATEST_SNAPSHOT
                if selected is not LATEST_SNAPSHOT:
                    metadata.snapshot_id = selected.filename if selected else None
            case self.arch_selection_view:
                architecture = self.arch_selection_view.selected_item if self.arch_selection_view.selected_item else None
                changed = architecture != self.project_directory.get_architecture()
                self.project_directory.initialize_metadata().architecture = architecture
                if changed:
                    GLib.idle_add(self._ask_packages_for_architecture)
            case self.packages_selection_view:
                self.project_directory.initialize_metadata().packages_directory_id = (
                    self.packages_selection_view.selected_item.id
                    if self.packages_selection_view.selected_item else None
                )
        Repository.ProjectDirectory.save()
        self._update_packages_warning()
        self._refresh_build_state() # Toolset could change.

    def on_new_packages_activated(self, row):
        from .packages_views import present_new_packages_directory_dialog
        from .packages_directory import project_cpu_flags, unique_packages_name
        def on_created(directory):
            self.packages_selection_view.select(directory)
        present_new_packages_directory_dialog(self, architecture=self.project_directory.get_architecture(),
                                              cpu_flags=project_cpu_flags(self.project_directory),
                                              name=unique_packages_name(self.project_directory.name), on_created=on_created)

    def _ask_packages_for_architecture(self):
        """Architecture changed: binary packages folder made for other architecture can't be used anymore."""
        directory = self.project_directory.get_packages_directory()
        if directory is None or directory.is_usable_by(self.project_directory):
            return False
        architecture = self.project_directory.get_architecture()
        dialog = Adw.AlertDialog(
            heading="Change binary packages folder?",
            body=f"{directory.name} has packages for {directory.architecture.name if directory.architecture else 'other architecture'}, "
                 f"they can't be used by {architecture.name if architecture else 'this architecture'}.")
        dialog.add_response("keep", "Keep")
        dialog.add_response("choose", "Choose other")
        dialog.add_response("new", "Create new")
        dialog.set_response_appearance("new", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("new")
        dialog.set_close_response("keep")
        def on_response(dialog, response):
            if response == "new":
                from .packages_directory import create_packages_directory_for_project
                self.packages_selection_view.select(create_packages_directory_for_project(self.project_directory))
            elif response == "choose":
                self.packages_selection_view.set_expanded(True)
        dialog.connect("response", on_response)
        dialog.present(self.get_root())
        return False

    def _update_name(self, name: str):
        self._page.set_title(name)

    def _update_stages(self, data):
        if not self._is_project_page_visible():
            self._stages_refresh_pending = True
            self._update_packages_warning()
            return
        self._stages_refresh_pending = False
        stages = self.project_directory.stages_tree()
        self.stages_compact_view.set_root_nodes(stages)
        self.stages_full_view.set_root_nodes(stages)
        self.stages_list_view.set_root_nodes(stages)
        self._refresh_stage_requirement_statuses_async()
        # Template/repository updates replace stage.json files and invalidate the
        # cached stages before emitting CONTENT_CHANGED. Recompute package-folder
        # compatibility against those refreshed stage flags as well.
        self._update_packages_warning()

    def _is_project_page_visible(self):
        navigation_view = self.content_navigation_view
        if navigation_view is None:
            return True
        page = navigation_view.get_visible_page()
        if page is None:
            return False
        child = page.get_child()
        return child is self or child.get_content() is self

    def _on_navigation_visible_page_changed(self, *_args):
        if self._stages_refresh_pending and self._is_project_page_visible():
            self._update_stages(self.project_directory)

    def _on_project_contents_reloaded(self, _project):
        self._update_stages(self.project_directory)
        self._update_source_banner()

    # Running build
    # --------------------------------------------------------------------------

    def _refresh_build_state(self):
        """Shows states of stages in running build of this project and button to its progress. Result of finished
        build isn't shown here, so it isn't seen (build progress and builds list show it)."""
        running_builds = _running_builds()
        for build in running_builds:
            if id(build) not in self._observed_builds:
                self._observed_builds.add(id(build))
                build.event_bus.subscribe(MultiStageProcessEvent.STATE_CHANGED, self._on_build_changed)
                for step in build.stages:
                    step.event_bus.subscribe(MultiStageProcessStageEvent.STATE_CHANGED, self._on_build_changed)
                    step.event_bus.subscribe(MultiStageProcessStageEvent.PROGRESS_CHANGED, self._on_build_changed)
        running_build = running_project_build(self.project_directory)
        # Only one build runs at a time, builds of other projects are added to queue meanwhile. Toolset (and its
        # virtual machine) can't be used by other operation while build runs.
        state = build_button_state(self.project_directory)
        # Buttons are in separate lists sharing one row, lists are hidden with them.
        self._build_action_available = state.visible
        self.build_row.set_sensitive(state.sensitive)
        self.build_row_label.set_label(state.title)
        self.build_row.set_tooltip_text(state.tooltip)
        self._build_running = running_build is not None
        self._update_action_rows_visibility()
        statuses = {}
        if running_build is not None:
            for step in running_build.stages:
                if isinstance(step, ProjectBuildStepBuildStage):
                    statuses[step.stage.id] = _build_step_status(step)
        for stage_id, requirement_status in self._stage_requirement_statuses.items():
            running_status = statuses.get(stage_id)
            if running_status:
                requirement_status = StageNodeStatus(
                    title=f"{requirement_status.title} · {running_status.title}",
                    icon_name=requirement_status.icon_name,
                    css_class=requirement_status.css_class,
                    in_progress=running_status.in_progress,
                )
            statuses[stage_id] = requirement_status
        self.stages_compact_view.set_statuses(statuses)
        self.stages_full_view.set_statuses(statuses)
        self.stages_list_view.set_statuses(statuses)

    def _load_stage_requirement_statuses(self, on_stage_checked=None, cancelled=None):
        """Use the same Catalyst required-value resolution as spec generation for stage warnings."""
        from .toolset_selection import default_toolset
        from .project_toolsets import get as get_project_toolset

        toolsets = self._project_catalyst_toolsets()
        if cancelled and cancelled.is_set():
            return {}
        fallback = default_toolset(applications=[ToolsetApplication.CATALYST], toolsets=toolsets)
        fallback = fallback or (toolsets[0] if toolsets else None)
        if not fallback:
            if on_stage_checked:
                for stage in self.project_directory.stages:
                    on_stage_checked(stage.id, None)
            return {}
        preferred_id = get_project_toolset(self.project_directory.directory_path())
        toolset = next((item for item in toolsets if str(item.uuid) == str(preferred_id)), fallback)
        snapshot = self.project_directory.get_snapshot()
        context_values = dict(
            timestamp="<timestamp>",
            source_subpath="<stage seed>",
            portage_confdir="<generated portage configuration>",
            root_overlay="<generated root overlay>",
            iso_overlay="<generated ISO overlay>",
            snapshot=snapshot,
            cache_paths={
                StageArgumentDetails.pkgcache_path: "<package cache>",
                StageArgumentDetails.kerncache_path: "<kernel cache>",
            },
        )
        # Resolve defaults for a detached copy of the full project in parent-first
        # order. A child can inherit a required value from a parent's toolset
        # defaults even when that parent has never been opened in the editor.
        evaluation_stages = []
        for original in self.project_directory.stages:
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
        evaluation_project = copy.copy(self.project_directory)
        evaluation_project.stages = evaluation_stages
        from .project_stage import refresh_default_stage_arguments

        statuses = {}
        for node, _depth in walk_tree(evaluation_project.stages_tree()):
            if cancelled and cancelled.is_set():
                return statuses
            stage = node.value
            try:
                refresh_default_stage_arguments(
                    evaluation_project, stage, toolset=toolset, snapshot=snapshot
                )
                missing = missing_required_stage_values(
                    evaluation_project, stage,
                    StageSpecContext(toolset=toolset, **context_values),
                )
            except Exception:
                # If Catalyst metadata cannot be read, leave the stage unmarked rather than guessing.
                if on_stage_checked:
                    on_stage_checked(stage.id, None)
                continue
            if cancelled and cancelled.is_set():
                return statuses
            status = None
            if missing:
                status = StageNodeStatus(
                    title=f"Missing required: {', '.join(missing)}",
                    icon_name="dialog-warning-symbolic",
                    css_class="warning",
                )
                statuses[stage.id] = status
            if on_stage_checked:
                on_stage_checked(stage.id, status)
        return statuses

    def _refresh_stage_requirement_statuses_async(self):
        """Run one cancellable requirement check at a time, keeping only the newest request."""
        if self._stage_requirement_cancel is not None:
            self._stage_requirement_cancel.set()
        if self._stage_requirement_future is not None:
            self._stage_requirement_future.cancel()
        self._stage_requirement_generation = getattr(self, "_stage_requirement_generation", 0) + 1
        generation = self._stage_requirement_generation
        cancelled = self._stage_requirement_cancel = threading.Event()
        stages = self.project_directory.stages
        self._stage_requirement_statuses = {
            stage.id: StageNodeStatus("Checking required settings…", in_progress=True)
            for stage in stages
        }
        self._refresh_build_state()

        def work():
            try:
                self._load_stage_requirement_statuses(
                    lambda stage_id, status: GLib.idle_add(
                        self._on_stage_requirement_checked, generation, stage_id, status
                    ),
                    cancelled=cancelled,
                )
            except Exception as exception:
                if cancelled.is_set():
                    return
                GLib.idle_add(
                    self._on_stage_requirement_check_failed,
                    generation,
                    [stage.id for stage in stages],
                    exception,
                )

        self._stage_requirement_future = self._stage_requirement_executor.submit(work)

    def _on_stage_requirement_checked(self, generation, stage_id, status):
        if generation != self._stage_requirement_generation:
            return GLib.SOURCE_REMOVE
        if status is None:
            self._stage_requirement_statuses.pop(stage_id, None)
        else:
            self._stage_requirement_statuses[stage_id] = status
        self._refresh_build_state()
        return GLib.SOURCE_REMOVE

    def _on_stage_requirement_check_failed(self, generation, stage_ids, error):
        if generation != self._stage_requirement_generation:
            return GLib.SOURCE_REMOVE
        print(f"Failed to check stage requirements: {error}")
        for stage_id in stage_ids:
            self._stage_requirement_statuses.pop(stage_id, None)
        self._refresh_build_state()
        return GLib.SOURCE_REMOVE

    def do_dispose(self):
        if self._stage_requirement_cancel is not None:
            self._stage_requirement_cancel.set()
        if self._stage_requirement_future is not None:
            self._stage_requirement_future.cancel()
        self._stage_requirement_executor.shutdown(wait=False, cancel_futures=True)
        Gtk.Widget.do_dispose(self)

    def _on_build_changed(self, *args):
        # Several changes can come at once, refresh once.
        if self._build_refresh_scheduled:
            return
        self._build_refresh_scheduled = True
        def refresh():
            self._build_refresh_scheduled = False
            self._refresh_build_state()
            return False
        GLib.idle_add(refresh)

    def _setup_source_banner(self):
        """Template or Git repository project was created from, own changes of project, and button updating project
        (blue when there are updates)."""
        self._updates_available = None # Checked in background when project is opened.
        self.project_directory.event_bus.subscribe(SharedEvent.STATE_UPDATED, self._update_source_banner)
        self._update_source_banner()
        self._check_updates()

    def _check_updates(self):
        """Template is checked for changes when project is opened (repositories are fetched with Git status), and again
        when project gets other version of template (after update)."""
        path = self.project_directory.directory_path()
        if (state := TemplateState.load(path)) is None:
            return
        self._checked_template_version = (state.commit, state.template_tree)
        def check():
            available = template_update_available(self.project_directory)
            def show():
                self._updates_available = available
                self._update_source_banner()
                return False
            GLib.idle_add(show)
        threading.Thread(target=check, daemon=True).start()

    def _update_source_banner(self, *args):
        path = self.project_directory.directory_path()
        self._source = None
        if state := TemplateState.load(path):
            self._source = "template"
            button = "Update template"
            details = f"{state.template_name}\n{state.repository_url}" + (f" ({state.repository_path})" if state.repository_path else "")
            if (state.commit, state.template_tree) != getattr(self, "_checked_template_version", None):
                # Template version of project changed (updated), result of previous check doesn't apply anymore.
                self._updates_available = None
                self._check_updates()
            updates = self._updates_available
        elif origin := repository_origin(path):
            self._source = "repository"
            button = "Update repository"
            details = origin.url + (f" ({origin.branch})" if origin.branch else "")
            updates = self.project_directory.has_remote_changes
        has_source = self._source is not None
        self._has_unsaved_changes = has_unsaved_changes(path)
        self._update_action_rows_visibility()
        if self._source is None:
            return
        unsaved = self._has_unsaved_changes
        self.source_button_label.set_label(button)
        # The action opens template options even when the source has no newer version;
        # only unsaved project changes prevent running an update.
        self.source_button.set_sensitive(not unsaved)
        if updates is True:
            self.source_button.add_css_class("source-updates-available")
            self.source_button_label.remove_css_class("dim-label")
        else:
            self.source_button.remove_css_class("source-updates-available")
            self.source_button_label.add_css_class("dim-label")
        self.source_button.set_tooltip_text(
            "Save or discard changes first" if unsaved
            else f"There are changes in {self._source}" if updates
            else f"No changes in {self._source} found" if updates is False
            else f"Check {self._source} for changes")
        self.source_button.set_tooltip_text(f"{self.source_button.get_tooltip_text()}\n{details}")

    @Gtk.Template.Callback()
    def _on_update_source_clicked(self, button):
        if self._source is None:
            return
        title = "Update template" if self._source == "template" else "Update from repository"
        source = self._source
        overlays = project_overlays(self.project_directory)
        def present(update_overlays: bool):
            view = ProjectUpdateView(self.project_directory, source, overlays=overlays if update_overlays else [])
            app_event_bus.emit(AppEvents.PRESENT_VIEW, view, title, 640, 560)
        if not overlays:
            present(False)
            return
        # Overlays used by project can be updated too.
        dialog = Adw.AlertDialog(heading=title, body=f"Latest version of {source} is downloaded and compared with project.")
        check_button = Gtk.CheckButton(label="Also update overlays used by project", active=True)
        names = Gtk.Label(label=", ".join(overlay.name for overlay in overlays), xalign=0, wrap=True, margin_start=28)
        names.add_css_class("caption")
        names.add_css_class("dimmed")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(check_button)
        box.append(names)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("update", "Update")
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("update")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _, response: present(check_button.get_active()) if response == "update" else None)
        dialog.present(self.get_root())

    def _setup_stages_view_mode(self):
        """Compact stages (short names, expanded while hovered) or full ones, switched in header bar while stages are
        shown."""
        self.stages_view_mode_toggle = CLToggleGroup(valign=Gtk.Align.CENTER)
        self.stages_view_mode_toggle.add_css_class("round")
        self.stages_view_mode_toggle.add_css_class("caption")
        self.stages_view_mode_toggle.add(CLToggle(label="Compact"))
        self.stages_view_mode_toggle.add(CLToggle(label="Full"))
        self.stages_view_mode_toggle.add(CLToggle(label="List"))
        mode_names = {"compact": 0, "full": 1, "list": 2}
        initial_mode = mode_names.get(Repository.Settings.value.project_stages_view_mode, 0)
        self.stages_view_mode_toggle.set_active(initial_mode)
        self._set_stage_view_mode(initial_mode, remember=False)
        self.stages_view_mode_toggle.connect("notify::active", lambda group, _: self._set_stage_view_mode(group.get_active()))
        self.stack.connect("notify::visible-child-name", lambda *args: self._update_stages_view_mode_visibility())
        self._update_stages_view_mode_visibility()

    def _set_stage_view_mode(self, mode: int, remember: bool = True):
        if remember:
            mode_names = {0: "compact", 1: "full", 2: "list"}
            Repository.Settings.value.project_stages_view_mode = mode_names.get(mode, "compact")
        if mode == 2:
            self.stage_content_stack.set_visible_child_name("list")
            return
        view_name = "full" if mode == 1 else "compact"
        self.stage_content_stack.set_visible_child_name(view_name)
        self.stages_compact_view.set_expand_all(False, remember=remember and mode == 0)
        self.stages_full_view.set_expand_all(True, remember=remember and mode == 1)

    def _update_stages_view_mode_visibility(self):
        is_stages_page = self.stack.get_visible_child_name() == "stages"
        self.stages_view_mode_toggle.set_visible(is_stages_page)
        self._update_action_rows_visibility()

    def _update_action_rows_visibility(self, *args):
        is_stages_page = self.stack.get_visible_child_name() == "stages"
        has_source = self._source is not None
        has_changes = getattr(self, "_has_unsaved_changes", self.project_directory.status == GitDirectoryStatus.CHANGED)
        active_update = any(
            update.directory is self.project_directory
            and update.status == MultiStageProcessState.IN_PROGRESS
            for update in MultiStageProcess.get_started_processes_by_class(GitUpdate)
        )
        self.add_stage_row.set_visible(is_stages_page)
        self.builds_row.set_visible(is_stages_page)
        self.build_row.set_visible(is_stages_page and self._build_action_available)
        self.build_progress_row.set_visible(is_stages_page and self._build_running)
        self.project_toolset_button.set_visible(is_stages_page)
        self.source_button.set_visible(is_stages_page and has_source)
        self.save_changes_button.set_visible(is_stages_page and has_changes)
        self.save_changes_button.set_sensitive(has_changes and not active_update)
        if has_changes:
            self.save_changes_button.add_css_class("source-updates-available")
        else:
            self.save_changes_button.remove_css_class("source-updates-available")
        self.source_action_row.set_visible(is_stages_page)
        self.stage_actions.set_margin_bottom(0 if has_source else 12)
        self.source_banner.set_visible(is_stages_page)

    def header_bar_end_widgets(self) -> list[Gtk.Widget]:
        return [self.stages_view_mode_toggle]

    def monitor_stages_changes(self):
        self.project_directory.event_bus.subscribe(
            GitDirectoryEvent.CONTENT_CHANGED,
            self._update_stages
        )
        self.project_directory.event_bus.subscribe(
            GitDirectoryEvent.CONTENT_RELOADED,
            self._on_project_contents_reloaded,
        )

    def monitor_information_changes(self):
        self.project_directory.event_bus.subscribe(
            GitDirectoryEvent.NAME_CHANGED,
            self._update_name
        )

    def monitor_configuration_changes(self):
        """Reacts to changes in configuration lists, saves new metadata"""
        self.project_directory.event_bus.subscribe(
            GitDirectoryEvent.CONFIGURATION_CHANGED,
            self._on_project_configuration_changed,
        )
        for view in [
            self.releng_selection_view,
            self.snapshot_selection_view,
            self.arch_selection_view,
            self.packages_selection_view
        ]:
            view.event_bus.subscribe(
                ItemSelectionViewEvent.ITEM_CHANGED,
                self.configuration_item_changed
            )

    def _on_project_configuration_changed(self, *_args):
        self.get_configuration()
        self._refresh_build_state()

    @Gtk.Template.Callback()
    def is_item_selectable(self, sender, item) -> bool:
        match sender:
            case self.releng_selection_view:
                return True
            case self.snapshot_selection_view:
                return True
            case self.arch_selection_view:
                return True
            case self.packages_selection_view:
                # Packages of other architecture can't be used.
                return item.is_usable_by(self.project_directory)
        return False

    @Gtk.Template.Callback()
    def is_item_usable(self, sender, item) -> bool:
        match sender:
            case self.releng_selection_view:
                return True
            case self.snapshot_selection_view:
                return True
            case self.arch_selection_view:
                return True
            case self.packages_selection_view:
                return True
        return False

    @Gtk.Template.Callback()
    def setup_items_monitoring(self, sender, items):
        return

    @Gtk.Template.Callback()
    def on_add_stage_activated(self, sender):
        if error := self.project_directory.configuration_error:
            self.show_alert(message=error)
            return
        app_event_bus.emit(AppEvents.PRESENT_VIEW, ProjectStageCreateView(project_directory=self.project_directory), "New Stage", 640, 480)

    @Gtk.Template.Callback()
    def on_build_activated(self, sender):
        if error := self.project_directory.configuration_error:
            self.show_alert(message=error)
            return
        state = build_button_state(self.project_directory)
        if state.queued is not None:
            present_queued_build(state.queued)
            return
        if not state.visible:
            self._refresh_build_state()
            return
        app_event_bus.emit(AppEvents.PRESENT_VIEW, ProjectBuildView(project_directory=self.project_directory), "Build stages", 640, 480)

    @Gtk.Template.Callback()
    def on_build_progress_activated(self, sender):
        if running_build := running_project_build(self.project_directory):
            app_event_bus.emit(AppEvents.PRESENT_VIEW, ProjectBuildView(project_directory=self.project_directory, installation_in_progress=running_build), "Build stages", 640, 480)
        else:
            self._refresh_build_state()

    @Gtk.Template.Callback()
    def on_builds_activated(self, sender):
        view = ProjectBuildsView(project_directory=self.project_directory, content_navigation_view=self.content_navigation_view)
        self.content_navigation_view.push_view(view, title=f"{self.project_directory.name} builds")

    @Gtk.Template.Callback()
    def on_stage_selected(self, sender, stage):
        if getattr(stage, "is_broken", False):
            return
        if error := self.project_directory.configuration_error:
            self.show_alert(message=error)
            return
        view = ProjectStageDetailsView(project_directory=self.project_directory, stage=stage, content_navigation_view=self.content_navigation_view)
        self.content_navigation_view.push_view(view, title=stage.name)

    def _close_after_delete(self):
        """Close this project page after its directory has been removed."""
        navigation_view = self.content_navigation_view
        page = getattr(self, "_page", None)
        if navigation_view is not None and page is not None:
            stack = navigation_view.get_navigation_stack()
            pages = [stack.get_item(index) for index in range(stack.get_n_items())]
            page_index = next((index for index, item in enumerate(pages) if item is page), None)
            if page_index is not None:
                if page_index > 0:
                    navigation_view.pop_to_page(pages[page_index - 1])
                    return
        # Project details can also be presented in a standalone window or through a
        # PushedViewHandle, where there is no page owned by this navigation view.
        if window_handle := getattr(self, "_window", None):
            close = getattr(window_handle, "close", None)
            if close:
                close()
                return
        if navigation_view is not None:
            navigation_view.pop()

    def show_alert(self, message):
        dialog = Gtk.MessageDialog(
            transient_for=self.get_root(),
            modal=True,
            buttons=Gtk.ButtonsType.CLOSE,
            text=message
        )
        dialog.connect("response", lambda d, r: d.destroy())
        dialog.show()

def _running_builds() -> list[ProjectBuild]:
    """Builds in progress, of all projects."""
    return [
        build for build in MultiStageProcess.get_started_processes_by_class(ProjectBuild)
        if build.status == MultiStageProcessState.IN_PROGRESS
    ]

def _build_step_status(step: ProjectBuildStepBuildStage) -> StageNodeStatus:
    match step.state:
        case MultiStageProcessStageState.IN_PROGRESS:
            # Progress of built packages.
            return StageNodeStatus(title=f"Building {int(step.progress * 100)}%" if step.progress is not None else "Building", in_progress=True)
        case MultiStageProcessStageState.COMPLETED:
            return StageNodeStatus(title="Built", icon_name="check-square-svgrepo-com-symbolic", css_class="success")
        case MultiStageProcessStageState.FAILED if getattr(step.multistage_process, "canceled", False):
            # Build was canceled: stopped while building, or not started.
            title = "Stopped" if step.build is not None else "Canceled"
            return StageNodeStatus(title=title, icon_name="stop-square-symbolic", css_class="dimmed")
        case MultiStageProcessStageState.FAILED if step.build is None:
            # Failed without starting, because stage it depends on failed.
            return StageNodeStatus(title="Skipped", icon_name="square-svgrepo-com-symbolic", css_class="dimmed")
        case MultiStageProcessStageState.FAILED:
            return StageNodeStatus(title="Failed", icon_name="error-box-svgrepo-com-symbolic", css_class="error")
    return StageNodeStatus(title="Scheduled", icon_name="clock-square-svgrepo-com-symbolic", css_class="dimmed")
