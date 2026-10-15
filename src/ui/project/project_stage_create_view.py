from __future__ import annotations
from gi.repository import Gtk, Adw, GLib
from .multistage_process import MultiStageProcessState
from .project_installation import ProjectInstallation
from .git_directory_create_config_view import GitDirectoryCreateConfigViewEvent
from .git_directory_default_content_builder import DefaultDirContentBuilder
from .git_installation import GitDirectorySetupConfiguration
from .toolset_application import ToolsetApplication
from .toolset import ToolsetEvents
from .wizard_view import WizardView
from .project_stage import stage_target_icon
from .toolset import Toolset
from .releng_directory import RelengDirectory
from .snapshot import Snapshot
from .project_stage import ProjectStage, load_catalyst_targets, load_releng_templates
from .project_stage_installation import ProjectStageInstallation
from .project_manager import ProjectManager
from .item_select_view import ItemRow
from .name_suggestion import unique_name
from enum import Enum, auto
import os, threading, uuid

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/project/project_stage_create_view.ui')
class ProjectStageCreateView(Gtk.Box):
    __gtype_name__ = "ProjectStageCreateView"

    # Main views:
    wizard_view = Gtk.Template.Child()
    # Setup view elements:
    spec_type_page = Gtk.Template.Child()
    releng_base_page = Gtk.Template.Child()
    options_page = Gtk.Template.Child()
    stage_name_row = Gtk.Template.Child()
    name_used_label = Gtk.Template.Child()
    spec_type_selection_view = Gtk.Template.Child()
    releng_base_selection_view = Gtk.Template.Child()
    seed_list_selection_view = Gtk.Template.Child()

    def __init__(self, project_directory: ProjectDirectory, installation_in_progress: ProjectInstallation | None = None, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__()
        self.project_directory = project_directory
        self.installation_in_progress = installation_in_progress
        self.content_navigation_view = content_navigation_view
        self.filename_is_free = False
        self.connect("realize", self.on_realize)

    def on_realize(self, widget):
        self.wizard_view.content_navigation_view = self.content_navigation_view
        self.wizard_view._window = self._window
        self.wizard_view.set_installation(self.installation_in_progress)
        self.load_targets()
        self.load_releng_templates()
        self.load_seed_list()
        self.monitor_information_changes()

    # Loading stage data
    # --------------------------------------------------------------------------

    def load_targets(self):
        values = load_catalyst_targets(toolset=self.project_directory.get_toolset())
        self.spec_type_selection_view.select(None)
        self.spec_type_selection_view.item_icon_resolver = stage_target_icon
        self.spec_type_selection_view.set_static_list(values)

    def load_releng_templates(self):
        releng_directory = self.project_directory.get_releng_directory()
        architecture = self.project_directory.get_architecture()
        template_subpaths = load_releng_templates(
            releng_directory=releng_directory,
            stage_name=self.spec_type_selection_view.selected_item,
            architecture=architecture
        ) if self.spec_type_selection_view.selected_item is not None and releng_directory is not None and architecture is not None else []
        self.releng_base_selection_view.select(None)
        self.releng_base_selection_view.set_static_list(template_subpaths)

    def load_seed_list(self):
        available_stages = self.project_directory.stages[:]
        is_stage_1 = self.spec_type_selection_view.selected_item and self.spec_type_selection_view.selected_item == "stage1"
        if is_stage_1:
            self.seed_list_selection_view.display_none = True
            self.seed_list_selection_view.none_title = "Download automatically"
            self.seed_list_selection_view.none_subtitle = "Latest stage3 from Gentoo mirrors is used as seed"
        values = available_stages
        selected = None
        self.seed_list_selection_view.select(selected)
        self.seed_list_selection_view.set_static_list(values)

    # Monitoring stage changes
    # --------------------------------------------------------------------------

    def monitor_information_changes(self):
        """React to changes in UI and store them."""
        self.spec_type_selection_view.connect("selection-changed", self.on_spec_type_changed)
        self.releng_base_selection_view.connect("selection-changed", self.on_releng_base_changed)
        self.seed_list_selection_view.connect("selection-changed", self.on_seed_selected)

    def on_spec_type_changed(self, sender, data):
        self.load_releng_templates()
        self.load_seed_list()
        self._update_default_stage_name()
        self.wizard_view._refresh_buttons_state()

    def on_releng_base_changed(self, sender, data):
        self._update_default_stage_name()
        self.wizard_view._refresh_buttons_state()

    def on_seed_selected(self, sender, data):
        self.wizard_view._refresh_buttons_state()

    # Handle UI
    # --------------------------------------------------------------------------

    @Gtk.Template.Callback()
    def is_page_ready_to_continue(self, sender, page) -> bool:
        match page:
            case self.spec_type_page:
                return self.spec_type_selection_view.selected_item is not None
            case self.releng_base_page:
                return True
            case self.options_page:
                is_stage_1 = self.spec_type_selection_view.selected_item and self.spec_type_selection_view.selected_item == "stage1"
                return self.filename_is_free and (self.seed_list_selection_view.selected_item or is_stage_1)
        return True

    @Gtk.Template.Callback()
    def is_item_selectable(self, sender, item) -> bool:
        match sender:
            case self.spec_type_selection_view:
                return True
            case self.releng_base_selection_view:
                return True
            case self.seed_list_selection_view:
                return True
        return False

    @Gtk.Template.Callback()
    def is_item_usable(self, sender, item) -> bool:
        match sender:
            case self.spec_type_selection_view:
                return True
            case self.releng_base_selection_view:
                return True
            case self.seed_list_selection_view:
                return True
        return False

    @Gtk.Template.Callback()
    def on_stage_name_activate(self, sender):
        self.check_filename_is_free()
        self.get_root().set_focus(None)

    @Gtk.Template.Callback()
    def on_stage_name_changed(self, sender):
        self.check_filename_is_free()

    @Gtk.Template.Callback()
    def begin_installation(self, view):
        self._start_installation(
            project_directory=self.project_directory,
            target_name=self.spec_type_selection_view.selected_item,
            releng_template_name=(
                self.releng_base_selection_view.selected_item
                if self.releng_base_selection_view.selected_item
                else None
            ),
            stage_name=self.stage_name_row.get_text(),
            parent_id=self.seed_list_selection_view.selected_item.id if self.seed_list_selection_view.selected_item else None
        )

    # Helper functions
    # --------------------------------------------------------------------------

    def check_filename_is_free(self) -> bool:
        self.filename_is_free = ProjectManager.shared().is_stage_name_available(project=self.project_directory, name=self.stage_name_row.get_text())
        self.name_used_label.set_visible(not self.filename_is_free)
        self.wizard_view._refresh_buttons_state()
        return self.filename_is_free

    def _update_default_stage_name(self):
        if self.releng_base_selection_view.selected_item is not None:
            name = os.path.splitext(os.path.basename(self.releng_base_selection_view.selected_item))[0]
        elif self.spec_type_selection_view.selected_item is not None:
            name = self.spec_type_selection_view.selected_item
        else:
            return
        self.stage_name_row.set_text(unique_name(
            name,
            lambda candidate: ProjectManager.shared().is_stage_name_available(project=self.project_directory, name=candidate),
        ))
        self.check_filename_is_free()

    def _start_installation(
        self,
        project_directory: ProjectDirectory,
        target_name: str,
        releng_template_name: str | None,
        stage_name: str,
        parent_id: uuid.UUID | None
    ):
        installation_in_progress = ProjectStageInstallation(
            project_directory=project_directory,
            target_name=target_name,
            releng_template_name=releng_template_name,
            stage_name=stage_name,
            parent_id=parent_id
        )
        installation_in_progress.start()
        self.wizard_view.set_installation(installation_in_progress)

