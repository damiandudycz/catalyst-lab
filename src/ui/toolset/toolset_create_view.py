from __future__ import annotations
import os, re, sys, threading
from gi.repository import Gtk, GLib, Gio
from gi.repository import Adw
from urllib.parse import ParseResult
from datetime import datetime
from pathlib import Path
from .rootless import authorize_toolset_action
from .lima import virtual_machines_supported
from .toolset_env_builder import get_stage3_urls
from .toolset_manager import ToolsetManager
from .architecture import Architecture
from .root_helper_client import AuthorizationKeeper
from .multistage_process import MultiStageProcessState
from .toolset_installation import ToolsetInstallation
from .toolset_application import ToolsetApplication, ToolsetApplicationSelection
from .wizard_view import WizardView
from .item_select_view import ItemSelectionViewEvent
from .cl_toggle_group import CLToggle, CLToggleGroup
from .name_suggestion import unique_name

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/toolset/toolset_create_view.ui')
class ToolsetCreateView(Gtk.Box):
    __gtype_name__ = "ToolsetCreateView"

    # Main views:
    wizard_view = Gtk.Template.Child()
    # Setup view elements:
    configuration_page = Gtk.Template.Child()
    runs_on_page = Gtk.Template.Child()
    machine_selection_view = Gtk.Template.Child()
    tools_page = Gtk.Template.Child()
    stages_list = Gtk.Template.Child()
    tools_list = Gtk.Template.Child()
    allow_binpkgs_checkbox = Gtk.Template.Child()
    environment_name_row = Gtk.Template.Child()
    name_used_label = Gtk.Template.Child()

    def __init__(self, installation_in_progress: ToolsetInstallation | None = None, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__()
        self.installation_in_progress = installation_in_progress
        self.content_navigation_view = content_navigation_view
        self.filename_is_free = False
        self.selected_stage: ParseResult | None = None
        self.architecture = Architecture.HOST
        self.allow_binpkgs = True
        self.tools_selection: Dict[ToolsetApplication, bool] = {app: not app.auto_select for app in ToolsetApplication.ALL}
        self.tools_selection_versions: Dict[ToolsetApplication, ToolsetApplicationSelection] = {app: app.versions[0] for app in ToolsetApplication.ALL}
        self.tools_selection_patches: Dict[ToolsetApplication, list[GLocalFile]] = {app: [] for app in ToolsetApplication.ALL}
        self.allow_binpkgs_checkbox.set_active(self.allow_binpkgs)
        self._load_applications_rows()
        # Toolsets run on this computer only on Linux, elsewhere only virtual machines are listed (first one is selected).
        self.local_unsupported_reason = None if sys.platform.startswith("linux") else "Not available on this system, select virtual machine"
        self.machine_selection_view.set_property("display_none", self.local_unsupported_reason is None)
        self.machine_selection_view.set_property("none_subtitle", "Runs directly on this computer")
        self.machine_selection_view.set_property("autoselect_default", self.local_unsupported_reason is not None)
        self.machine_selection_view.event_bus.subscribe(ItemSelectionViewEvent.ITEM_CHANGED, lambda view: self.wizard_view._refresh_buttons_state())
        if not virtual_machines_supported():
            self.wizard_view.remove_page(self.runs_on_page) # Toolsets run on this computer.
        if installation_in_progress is None or installation_in_progress.status == MultiStageProcessState.SETUP:
            threading.Thread(target=self._load_stage3_urls, daemon=True).start()
        self.connect("realize", self.on_realize)

    def on_realize(self, widget):
        self.wizard_view.content_navigation_view = self.content_navigation_view
        self.wizard_view._window = self._window
        self.wizard_view.set_installation(self.installation_in_progress)

    @Gtk.Template.Callback()
    def is_page_ready_to_continue(self, sender, page) -> bool:
        match page:
            case self.runs_on_page:
                return self.machine_selection_view.selected_item is not None or self.local_unsupported_reason is None
            case self.configuration_page:
                return self.selected_stage is not None
            case self.tools_page:
                return self.filename_is_free
        return True

    @Gtk.Template.Callback()
    def begin_installation(self, view):
        authorize_toolset_action(callback=lambda authorization_keeper: self._start_installation(authorization_keeper=authorization_keeper),
                                 machine=self.machine_selection_view.selected_item)

    @Gtk.Template.Callback()
    def is_machine_selectable(self, sender, machine) -> bool:
        return True

    @Gtk.Template.Callback()
    def on_allow_binpkgs_toggled(self, checkbox):
        self.allow_binpkgs = checkbox.get_active()

    def _start_installation(self, authorization_keeper: AuthorizationKeeper):
        if not authorization_keeper:
            return
        apps_selection = [
            ToolsetApplicationSelection(
                app=app,
                version=self.tools_selection_versions[app],
                selected=self.tools_selection[app],
                patches=self.tools_selection_patches[app]
            )
            for app, _ in self.tools_selection.items()
        ]
        installation_in_progress = ToolsetInstallation(
            alias=self.environment_name_row.get_text(),
            stage_url=self.selected_stage,
            allow_binpkgs=self.allow_binpkgs,
            apps_selection=apps_selection,
            machine=self.machine_selection_view.selected_item
        )
        installation_in_progress.start()
        self.wizard_view.set_installation(installation_in_progress)

    def _update_stages_result(self, result: list[ParseResult] | Exception):
        self.selected_stage = None
        if hasattr(self, "_stage_rows"):
            for row in self._stage_rows:
                self.stages_list.remove(row)
        # Refresh list of results
        if isinstance(result, Exception):
            error_label = Gtk.Label(label=f"Error: {str(result)}")
            error_label.set_wrap(True)
            error_label.set_halign(Gtk.Align.CENTER)
            error_label.set_margin_top(12)
            error_label.set_margin_bottom(12)
            error_label.set_margin_start(24)
            error_label.set_margin_end(24)
            error_label.add_css_class("dimmed")
            self.stages_list.add(error_label)
            self._stage_rows = [error_label]
        else:
            if result:
                # Place recommended first.
                sorted_stages = sorted(result, key=lambda stage: not ToolsetCreateView._is_recommended_stage(
                    os.path.basename(stage.geturl()), self.architecture
                ))
                self.selected_stage = sorted_stages[0]
                self.environment_name_row.set_text(self.default_name())
                self.check_filename_is_free()
            self._stage_rows = []
            stages_check_buttons_group = []
            for stage in sorted_stages:
                filename = os.path.basename(stage.geturl())
                row = Adw.ActionRow(title=filename)
                check_button = Gtk.CheckButton()
                check_button.set_active(stage == self.selected_stage)
                if stages_check_buttons_group:
                    check_button.set_group(stages_check_buttons_group[0])
                if ToolsetCreateView._is_recommended_stage(filename, self.architecture):
                    # Mark first entry as recommended
                    label = Gtk.Label(label="Recommended")
                    label.add_css_class("dim-label")
                    label.add_css_class("caption")
                    row.add_suffix(label)
                check_button.connect("toggled", self._on_stage_selected, stage)
                stages_check_buttons_group.append(check_button)
                row.add_prefix(check_button)
                row.set_activatable_widget(check_button)
                self.stages_list.add(row)
                self._stage_rows.append(row)

    def _load_stage3_urls(self):
        result = get_stage3_urls(self.architecture)
        GLib.idle_add(self._update_stages_result, result)

    def _on_stage_selected(self, button: Gtk.CheckButton, stage: ParseResult):
        """Callback for when a row's checkbox is toggled."""
        if button.get_active():
            self.selected_stage = stage
            self.environment_name_row.set_text(self.default_name())
            self.check_filename_is_free()
        else:
            # Deselect if unchecked
            if self.selected_stage == stage:
                self.selected_stage = None
        self.wizard_view._refresh_buttons_state()

    def _on_tool_selected(self, button: Gtk.CheckButton, app: ToolsetApplication, row: Adw.ExpanderRow):
        """Callback for when a row's checkbox is toggled."""
        self.tools_selection[app] = button.get_active()
        self.wizard_view._refresh_buttons_state()
        self._update_dependencies()
        row.set_enable_expansion(self.tools_selection[app])
        row.set_expanded(False)

    @staticmethod
    def _is_recommended_stage(filename: str, architecture: Architecture) -> bool:
        escaped_arch = re.escape(architecture.value)
        pattern = rf'^stage3-{escaped_arch}-openrc-(\d{{8}}T\d{{6}}Z)\.tar\.xz$'
        match = re.match(pattern, filename)
        if not match:
            return False
        date_str = match.group(1)
        try:
            datetime.strptime(date_str, "%Y%m%dT%H%M%SZ")
            return True
        except ValueError:
            return False

    def _update_application_info_label(self, row: Adw.ExpanderRow, app):
        fragments = [self.tools_selection_versions[app].name]
        if self.tools_selection_patches[app]:
            fragments.append("Patched")
        row.info_label.set_label(", ".join(fragments))

    def _load_applications_rows(self):
        self.tools_rows = {}
        for app in ToolsetApplication.ALL:
            if app.auto_select:
                continue # Don't show automatic dependencies
            row = Adw.ExpanderRow(title=app.name)
            row.set_subtitle(app.description)
            row.set_enable_expansion(self.tools_selection[app])

            check_button = Gtk.CheckButton()
            check_button.set_active(self.tools_selection[app])
            check_button.connect("toggled", self._on_tool_selected, app, row)
            row.check_button = check_button
            row.add_prefix(check_button)

            label = Gtk.Label()
            label.add_css_class("dim-label")
            label.add_css_class("caption")
            row.info_label = label
            row.add_suffix(label)

            versions_row = Adw.ActionRow()
            versions_row.set_activatable(False)

            toggle_group = CLToggleGroup()
            toggle_group.add_css_class("round")
            toggle_group.add_css_class("caption-heading")
            for version in app.versions:
                toggle = CLToggle(label=version.name)
                toggle_group.add(toggle)
            def on_toggle_clicked(group, pspec, app, row):
                index = group.get_active()
                self.tools_selection_versions[app] = app.versions[index]
                self._update_application_info_label(row=row, app=app)
            toggle_group.connect("notify::active", on_toggle_clicked, app, row)
            wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
            wrapper.append(toggle_group)

            versions_row.add_prefix(wrapper)
            row.add_row(versions_row)

            add_patch_button = Gtk.Button()
            add_patch_button_content = Adw.ButtonContent(label="Add patch", icon_name="copy-svgrepo-com-symbolic")
            add_patch_button.set_child(add_patch_button_content)
            add_patch_button.get_style_context().add_class("flat")
            add_patch_button.get_style_context().add_class("caption")
            add_patch_button.connect("clicked", self._on_add_patch_clicked, app, row)
            wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
            wrapper.append(add_patch_button)
            versions_row.add_suffix(wrapper)

            self._update_application_info_label(row=row, app=app)
            self.tools_list.add(row)
            self.tools_rows[app] = row
        # After building all rows, update sensitivity based on current state
        self._update_dependencies()

    def _on_add_patch_clicked(self, add_patch_row, app, app_row):
        def delete_patch_pressed(remove_button, app, app_row, patch_row):
            self.tools_selection_patches[app].remove(remove_button.file)
            app_row.remove(patch_row)
            self._update_application_info_label(row=app_row, app=app)
        def on_file_open_response(file_dialog, result):
            try:
                file = file_dialog.open_finish(result)
                self.tools_selection_patches[app].append(file)
                patch_row = Adw.ActionRow(title=file.get_basename(), subtitle="Patch file", icon_name="copy-svgrepo-com-symbolic")
                remove_button = Gtk.Button()
                remove_button_content = Adw.ButtonContent(label="Remove", icon_name="error-box-svgrepo-com-symbolic")
                remove_button.set_child(remove_button_content)
                remove_button.get_style_context().add_class("destructive-action")
                remove_button.get_style_context().add_class("flat")
                remove_button.get_style_context().add_class("caption")
                remove_button.connect("clicked", delete_patch_pressed, app, app_row, patch_row)
                remove_button.file = file
                wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
                wrapper.append(remove_button)
                patch_row.add_suffix(wrapper)
                app_row.add_row(patch_row)
                self._update_application_info_label(row=app_row, app=app)
            except GLib.Error as e:
                print("File open canceled or failed:", e)
        def create_patch_file_filter():
            file_filter = Gtk.FileFilter()
            file_filter.set_name("Patch files (*.patch)")
            file_filter.add_pattern("*.patch")
            return file_filter
        def create_filter_list():
            store = Gio.ListStore.new(Gtk.FileFilter)
            store.append(create_patch_file_filter())
            return store
        file_dialog = Gtk.FileDialog.new()
        file_dialog.set_title("Select a .patch file")
        filters = create_filter_list()
        file_dialog.set_filters(filters)
        file_dialog.open(self.get_root(), None, on_file_open_response)

    def _update_dependencies(self):
        for app, row in self.tools_rows.items():
            # Create the comma-separated string of unmet dependencies
            unmet_dependencies = ", ".join([
                dep.name for dep in getattr(app, "dependencies", ())
                if not (self.tools_selection[dep] or dep.auto_select)
            ])
            if unmet_dependencies:
                 row.set_title(f"{app.name} (requires: {unmet_dependencies})")
            else:
                row.set_title(app.name)
            # Check if all dependencies are satisfied (i.e., no unmet dependencies)
            dependencies_satisfied = not unmet_dependencies
            is_sensitive = dependencies_satisfied
            row.set_sensitive(is_sensitive)
            if not is_sensitive:
                row.set_expanded(False)
            self.tools_rows[app].check_button.set_sensitive(is_sensitive)

    @Gtk.Template.Callback()
    def on_environment_name_activate(self, sender):
        self.check_filename_is_free()
        self.get_root().set_focus(None)

    @Gtk.Template.Callback()
    def on_environment_name_changed(self, sender):
        self.check_filename_is_free()

    def check_filename_is_free(self) -> bool:
        self.filename_is_free = ToolsetManager.shared().is_name_available(name=self.environment_name_row.get_text())
        self.name_used_label.set_visible(not self.filename_is_free)
        self.wizard_view._refresh_buttons_state()
        return self.filename_is_free

    def default_name(self) -> str:
        if self.selected_stage is None:
            return ""
        file_path = Path(self.selected_stage.path)
        suffixes = file_path.suffixes
        filename_without_extension = file_path.stem
        for suffix in suffixes:
            filename_without_extension = filename_without_extension.rstrip(suffix)
        parts = filename_without_extension.split("-")
        if len(parts) > 2:
            middle_parts = parts[1:-1]
            installer_name = " ".join(middle_parts)
        else:
            installer_name = filename_without_extension
        return unique_name(installer_name, ToolsetManager.shared().is_name_available)

