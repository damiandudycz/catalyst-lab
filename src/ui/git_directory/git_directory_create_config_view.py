from __future__ import annotations
from gi.repository import Gtk, GLib, Gio, Adw, GObject
from enum import Enum, auto
from .event_bus import EventBus
from .git_installation import GitDirectorySetupConfiguration, GitDirectorySource
from .git_directory_default_content_builder import DefaultDirContentBuilder
from .cl_toggle_group import CLToggle, CLToggleGroup

# Import additional classed so that it can be parsed in repository_list_view:
from .releng_manager import RelengManager
from .overlay_manager import OverlayManager
from .project_manager import ProjectManager

class GitDirectoryCreateConfigViewEvent(Enum):
    CONFIGURATION_READY_CHANGED = auto()

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/git_directory/git_directory_create_config_view.ui')
class GitDirectoryCreateConfigView(Gtk.Box):
    __gtype_name__ = "GitDirectoryCreateConfigView"

    source_group = Gtk.Template.Child()
    options_group = Gtk.Template.Child()
    source_toggle_group_container = Gtk.Template.Child()
    directory_name_row = Gtk.Template.Child()
    directory_local_directory_row = Gtk.Template.Child()
    directory_local_directory_button = Gtk.Template.Child()
    directory_url_row = Gtk.Template.Child()
    name_used_label = Gtk.Template.Child()
    template_container = Gtk.Template.Child()

    manager_class_name = GObject.Property(type=str, default=None)
    available_sources = GObject.Property(type=str, default=None)
    default_git_repository = GObject.Property(type=str, default=None)
    default_local_directory = GObject.Property(type=str, default=None)
    default_directory_name = GObject.Property(type=str, default=None)

    allow_changing_git_repository = GObject.Property(type=bool, default=True)
    allow_changing_local_directory = GObject.Property(type=bool, default=True)
    allow_changing_directory_name = GObject.Property(type=bool, default=True)

    def __init__(self):
        super().__init__()
        self.event_bus = EventBus[GitDirectoryCreateConfigViewEvent]()
        self.configuration_ready = False
        self.template_chooser = None
        self._suggested_directory_name = None
        self.connect("realize", self.on_realize)

    def set_template_chooser(self, template_chooser: Gtk.Widget):
        """Widget selecting template for TEMPLATE source (eg. ProjectTemplateChooser). It has selected_template
        (with name) and calls on_changed when selection changes."""
        self.template_chooser = template_chooser
        self.template_container.append(template_chooser)
        previous_on_changed = template_chooser.on_changed
        def on_changed():
            template = template_chooser.selected_template
            if template is not None:
                self.suggest_directory_name(template.name)
            self.check_if_configuration_ready()
            if previous_on_changed:
                previous_on_changed()
        template_chooser.on_changed = on_changed

    def suggest_directory_name(self, name: str):
        """Sets directory name, unless user entered own name."""
        if not self.allow_changing_directory_name:
            return
        current = self.directory_name_row.get_text()
        if current in ("", self.default_directory_name, self._suggested_directory_name):
            self._suggested_directory_name = name
            self.directory_name_row.set_text(name)

    def on_realize(self, widget):
        self.manager_class = globals().get(self.manager_class_name)
        self.setup_source_toggles()
        self._update_source_rows()
        self.directory_url_row.set_text(self.default_git_repository or "")
        self.selected_local_directory: Gio.File | None = Gio.File.new_for_path(self.default_local_directory) if self.default_local_directory else None
        self.directory_local_directory_row.set_subtitle(self.selected_local_directory.get_path() if self.selected_local_directory else "(Select directory)")
        self.directory_name_row.set_text(self.default_directory_name if self.default_directory_name else "")
        self.directory_url_row.set_editable(self.allow_changing_git_repository)
        self.directory_url_row.set_sensitive(self.allow_changing_git_repository)
        self.directory_local_directory_button.set_visible(self.allow_changing_git_repository)
        self.directory_local_directory_button.set_sensitive(self.allow_changing_git_repository)
        self.directory_name_row.set_editable(self.allow_changing_directory_name)
        self.directory_name_row.set_sensitive(self.allow_changing_directory_name)
        self.check_if_configuration_ready()

    def get_configuration(self, default_dir_content_builder: DefaultDirContentBuilder | None = None, template_selection=None) -> GitDirectorySetupConfiguration:
        match self.selected_source:
            case GitDirectorySource.GIT_REPOSITORY:
                data = self.directory_url_row.get_text()
            case GitDirectorySource.LOCAL_DIRECTORY:
                data = self.selected_local_directory
            case GitDirectorySource.CREATE_NEW:
                data = default_dir_content_builder
            case GitDirectorySource.TEMPLATE:
                data = template_selection
        return GitDirectorySetupConfiguration(
            source=self.selected_source,
            name=self.directory_name_row.get_text(),
            data=data
        )

    def setup_source_toggles(self):
        toggle_group = CLToggleGroup()
        toggle_group.add_css_class("round")
        toggle_group.add_css_class("caption")

        sources = [GitDirectorySource[value.strip()] for value in self.available_sources.split(",")]

        self.selected_source = sources[0]
        self.source_group.set_visible(len(sources) > 1)
        for index, source in enumerate(sources):
            toggle = CLToggle(label=source.name())
            toggle_group.add(toggle)
            if source == self.selected_source:
                toggle_group.set_active(index)
        def on_toggle_source_clicked(group, pspec):
            index = group.get_active()
            self.directory_url_row.set_text(self.default_git_repository or "")
            self.selected_local_directory = Gio.File.new_for_path(self.default_local_directory) if self.default_local_directory else None
            self.directory_local_directory_row.set_subtitle(self.selected_local_directory.get_path() if self.selected_local_directory else "(Select directory)")
            self.selected_source = sources[index]
            self._update_source_rows()
            self.check_if_configuration_ready()
        toggle_group.connect("notify::active", on_toggle_source_clicked)
        self.source_toggle_group_container.append(toggle_group)

    def _update_source_rows(self):
        self.directory_local_directory_row.set_visible(self.selected_source == GitDirectorySource.LOCAL_DIRECTORY)
        self.directory_url_row.set_visible(self.selected_source == GitDirectorySource.GIT_REPOSITORY)
        self.template_container.set_visible(self.selected_source == GitDirectorySource.TEMPLATE)

    def check_filename_is_free(self) -> bool:
        self.filename_is_free = self.manager_class.shared().is_name_available(name=self.directory_name_row.get_text())
        self.name_used_label.set_visible(not self.filename_is_free)
        return self.filename_is_free

    def check_source_is_configured(self) -> bool:
        match self.selected_source:
            case GitDirectorySource.GIT_REPOSITORY:
                self.source_is_configured = self.directory_url_row.get_text()
            case GitDirectorySource.LOCAL_DIRECTORY:
                self.source_is_configured = self.selected_local_directory
            case GitDirectorySource.CREATE_NEW:
                self.source_is_configured = True
            case GitDirectorySource.TEMPLATE:
                self.source_is_configured = self.template_chooser is not None and self.template_chooser.selected_template is not None
        return self.source_is_configured

    def check_if_configuration_ready(self) -> bool:
        self.configuration_ready = self.check_filename_is_free() and self.check_source_is_configured()
        self.event_bus.emit(GitDirectoryCreateConfigViewEvent.CONFIGURATION_READY_CHANGED, self.configuration_ready)
        return self.configuration_ready

    @Gtk.Template.Callback()
    def on_directory_name_activate(self, sender):
        self.check_if_configuration_ready()
        self.get_root().set_focus(None)

    @Gtk.Template.Callback()
    def on_directory_name_changed(self, sender):
        self.check_if_configuration_ready()

    @Gtk.Template.Callback()
    def on_directory_url_activate(self, sender):
        self.get_root().set_focus(None)
        if self.allow_changing_directory_name:
            self.directory_name_row.set_text(self.directory_url_row.get_text().rstrip('/').split('/')[-1])
        self.check_if_configuration_ready()

    @Gtk.Template.Callback()
    def on_directory_url_changed(self, sender):
        self.check_if_configuration_ready()

    @Gtk.Template.Callback()
    def _on_select_local_directory_clicked(self, sender):
        def on_folder_selected(dialog, result):
            try:
                self.selected_local_directory = dialog.select_folder_finish(result)
                if self.allow_changing_directory_name:
                    self.directory_name_row.set_text(self.selected_local_directory.get_basename())
                self.directory_local_directory_row.set_subtitle(self.selected_local_directory.get_path())
                self.check_if_configuration_ready()
                print("Selected folder:", self.selected_local_directory.get_path())
            except GLib.Error as e:
                print("Folder selection canceled or failed:", e)
        file_dialog = Gtk.FileDialog.new()
        file_dialog.set_title("Select a directory")
        file_dialog.select_folder(
            self.get_root(),
            None,
            on_folder_selected
        )

