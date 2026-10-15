from __future__ import annotations
from gi.repository import Gtk, Adw
from .multistage_process import MultiStageProcessState
from .project_installation import ProjectInstallation
from .git_directory_create_config_view import GitDirectoryCreateConfigViewEvent
from .git_directory_default_content_builder import DefaultDirContentBuilder
from .git_installation import GitDirectorySetupConfiguration, GitDirectorySource
from .project_template import ProjectTemplateSelection
from .project_template_views import ProjectTemplateChooser, ProjectTemplateOptionsView
from .toolset_application import ToolsetApplication
from .toolset import ToolsetEvents
from .wizard_view import WizardView
from .item_select_view import ItemSelectionViewEvent
from .architecture import Architecture
from .snapshot import LATEST_SNAPSHOT
import os

class DefaultProjectDirContentBuilder(DefaultDirContentBuilder):
    def build_in(self, path: str, repo_name: str):
        structure = ['stages']
        for folder in structure:
            os.makedirs(os.path.join(path, folder), exist_ok=True)

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/project/project_create_view.ui')
class ProjectCreateView(Gtk.Box):
    __gtype_name__ = "ProjectCreateView"

    # Main views:
    wizard_view = Gtk.Template.Child()
    # Setup view elements:
    source_page = Gtk.Template.Child()
    source_view = Gtk.Template.Child()
    template_page = Gtk.Template.Child()
    template_options_container = Gtk.Template.Child()
    toolset_page = Gtk.Template.Child()
    toolset_selection_view = Gtk.Template.Child()
    releng_page = Gtk.Template.Child()
    releng_selection_view = Gtk.Template.Child()
    snapshot_page = Gtk.Template.Child()
    snapshot_selection_view = Gtk.Template.Child()
    arch_page = Gtk.Template.Child()
    arch_selection_view = Gtk.Template.Child()
    packages_page = Gtk.Template.Child()
    packages_selection_view = Gtk.Template.Child()

    def __init__(self, installation_in_progress: ProjectInstallation | None = None, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__()
        self.installation_in_progress = installation_in_progress
        self.content_navigation_view = content_navigation_view
        self.apps_requirements = [ToolsetApplication.CATALYST]
        self.arch_selection_view.set_static_list(sorted(Architecture, key=lambda arch: arch.name))
        # Latest snapshot (default) is generated with toolset of project when building.
        self.snapshot_selection_view.set_leading_items([LATEST_SNAPSHOT])
        # Templates: chooser in source page, options of selected template in next page.
        self.template_chooser = ProjectTemplateChooser()
        self.template_options_view = ProjectTemplateOptionsView()
        self.template_options_container.append(self.template_options_view)
        self.template_chooser.on_changed = self.template_changed
        self.template_options_view.on_changed = self.template_options_changed
        self.source_view.set_template_chooser(self.template_chooser)
        self.source_view.event_bus.subscribe(
            GitDirectoryCreateConfigViewEvent.CONFIGURATION_READY_CHANGED,
            self.config_ready_changed
        )
        self.toolset_selection_view.event_bus.subscribe(
            ItemSelectionViewEvent.ITEM_CHANGED,
            self.toolset_changed
        )
        self.releng_selection_view.event_bus.subscribe(
            ItemSelectionViewEvent.ITEM_CHANGED,
            self.releng_changed
        )
        self.snapshot_selection_view.event_bus.subscribe(
            ItemSelectionViewEvent.ITEM_CHANGED,
            self.snapshot_changed
        )
        self.arch_selection_view.event_bus.subscribe(
            ItemSelectionViewEvent.ITEM_CHANGED,
            self.arch_changed
        )
        self.connect("realize", self.on_realize)

    def config_ready_changed(self, data):
        self._update_pages()
        self.wizard_view._refresh_buttons_state()

    def template_changed(self):
        self.template_options_view.set_template(self.template_chooser.selected_template)
        self._update_pages()

    def template_options_changed(self):
        self.packages_selection_view.refresh_items_state(None) # Template can set architecture.
        self.wizard_view._refresh_buttons_state()

    @property
    def _uses_template(self) -> bool:
        return getattr(self.source_view, "selected_source", None) == GitDirectorySource.TEMPLATE

    def _update_pages(self):
        """Template options page is shown for template source, architecture page unless template sets it (fixed or with
        architecture variable)."""
        template = self.template_chooser.selected_template if self._uses_template else None
        self.wizard_view.set_page_visible(self.template_page, self._uses_template)
        self.wizard_view.set_page_visible(self.arch_page, template is None or (template.architecture_variable is None and template.architecture is None))

    def toolset_changed(self, data):
        self.snapshot_selection_view.refresh_items_state(None) # Latest snapshot is generated with toolset (it needs Catalyst).
        self.wizard_view._refresh_buttons_state()

    def releng_changed(self, data):
        self.wizard_view._refresh_buttons_state()

    def snapshot_changed(self, data):
        self.wizard_view._refresh_buttons_state()

    def arch_changed(self, data):
        self.packages_selection_view.refresh_items_state(None) # Folders depend on architecture.
        self.wizard_view._refresh_buttons_state()

    def _selected_architecture(self):
        """Architecture of new project: from template (when it sets it) or architecture page."""
        if self._uses_template and self.template_options_view.generated and self.template_options_view.generated.architecture:
            return self.template_options_view.generated.architecture
        return self.arch_selection_view.selected_item

    def on_realize(self, widget):
        self.wizard_view.content_navigation_view = self.content_navigation_view
        self.wizard_view._window = self._window
        self.wizard_view.set_installation(self.installation_in_progress)
        self._update_pages()

    @Gtk.Template.Callback()
    def is_item_selectable(self, sender, item) -> bool:
        match sender:
            case self.toolset_selection_view:
                return all(item.get_app_install(app) is not None for app in self.apps_requirements)
            case self.releng_selection_view:
                return True
            case self.snapshot_selection_view:
                if item is LATEST_SNAPSHOT:
                    toolset = self.toolset_selection_view.selected_item
                    return toolset is None or toolset.get_app_install(ToolsetApplication.CATALYST) is not None
                return True
            case self.arch_selection_view:
                return True
            case self.packages_selection_view:
                # Packages of other architecture can't be used.
                architecture = self._selected_architecture()
                return item.architecture is None or architecture is None or item.architecture == architecture
        return False

    @Gtk.Template.Callback()
    def is_item_usable(self, sender, item) -> bool:
        match sender:
            case self.toolset_selection_view:
                return True # Project only refers to toolset, it can be used by other process at this time.
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
        match sender:
            case self.toolset_selection_view:
                if hasattr(self, 'monitored_items_toolset'):
                    for item in self.monitored_items_toolset:
                        item.event_bus.unsubscribe(ToolsetEvents.IS_RESERVED_CHANGED, self)
                self.monitored_items_toolset = items
                for item in items:
                    item.event_bus.subscribe(
                        ToolsetEvents.IS_RESERVED_CHANGED,
                        self.toolset_selection_view.refresh_items_state,
                        self
                    )
            case self.releng_selection_view:
                pass
            case self.snapshot_selection_view:
                pass
            case self.arch_selection_view:
                pass

    @Gtk.Template.Callback()
    def is_page_ready_to_continue(self, sender, page) -> bool:
        match page:
            case self.source_page:
                return self.source_view.configuration_ready
            case self.template_page:
                return self.template_options_view.generated is not None
            case self.toolset_page:
                return (
                    self.toolset_selection_view.selected_item is not None
                    and self.is_item_usable(self.toolset_selection_view, self.toolset_selection_view.selected_item)
                    and self.is_item_selectable(self.toolset_selection_view, self.toolset_selection_view.selected_item)
                )
            case self.releng_page:
                return self.releng_selection_view.selected_item is not None
            case self.snapshot_page:
                return self.snapshot_selection_view.selected_item is not None
            case self.arch_page:
                return self.arch_selection_view.selected_item is not None
        return True

    @Gtk.Template.Callback()
    def begin_installation(self, view):
        template_selection = None
        architecture = self.arch_selection_view.selected_item
        if self._uses_template:
            template_selection = ProjectTemplateSelection(
                template=self.template_chooser.selected_template,
                selected=dict(self.template_options_view.selected)
            )
            architecture = self.template_options_view.generated.architecture or architecture
        self._start_installation(
            source_config=self.source_view.get_configuration(
                default_dir_content_builder=DefaultProjectDirContentBuilder(),
                template_selection=template_selection
            ),
            toolset=self.toolset_selection_view.selected_item,
            releng_directory=self.releng_selection_view.selected_item,
            snapshot=self.snapshot_selection_view.selected_item,
            architecture=architecture,
            packages_directory=self._usable_packages_directory(architecture)
        )

    def _usable_packages_directory(self, architecture):
        """Selected binary packages folder, None (new folder is created) when it doesn't match architecture."""
        directory = self.packages_selection_view.selected_item
        if directory is not None and architecture is not None and directory.architecture not in (None, architecture):
            return None
        return directory

    def _start_installation(
        self,
        source_config: GitDirectorySetupConfiguration,
        toolset: Toolset,
        releng_directory: RelengDirectory,
        snapshot: Snapshot,
        architecture: Architecture,
        packages_directory=None
    ):
        installation_in_progress = ProjectInstallation(
            source_config=source_config,
            toolset=toolset,
            releng_directory=releng_directory,
            snapshot=snapshot,
            architecture=architecture,
            packages_directory=packages_directory
        )
        installation_in_progress.start()
        self.wizard_view.set_installation(installation_in_progress)

