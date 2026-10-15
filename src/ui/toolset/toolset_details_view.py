import sys
from gi.repository import Gtk, Adw, Gio, GLib
from .rootless import authorize_toolset_action
from .lima import virtual_machines_supported
from .toolset import Toolset, ToolsetEvents
from .toolset_application import ToolsetApplication, ToolsetApplicationSelection
from .helper_functions import get_file_size_string
import os
from datetime import datetime
from urllib.parse import urlparse
from .root_helper_client import RootHelperClient, AuthorizationKeeper
from .repository import Repository
from .toolset_update import ToolsetUpdate
from .multistage_process import MultiStageProcess, MultiStageProcessEvent, MultiStageProcessState
from .multistage_process_execution_view import MultistageProcessExecutionView
from .toolset_manager import ToolsetManager
from .cl_toggle_group import CLToggle, CLToggleGroup
from .combo_row_popup import fit_popup_to_items

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/toolset/toolset_details_view.ui')
class ToolsetDetailsView(Gtk.Box):
    __gtype_name__ = "ToolsetDetailsView"

    toolset_name_row = Gtk.Template.Child()
    name_used_row = Gtk.Template.Child()
    applications_group = Gtk.Template.Child()
    status_file_row = Gtk.Template.Child()
    status_size_row = Gtk.Template.Child()
    runs_on_row = Gtk.Template.Child()
    toolset_date_created_row = Gtk.Template.Child()
    toolset_date_updated_row = Gtk.Template.Child()
    toolset_source_row = Gtk.Template.Child()
    status_bindings_row = Gtk.Template.Child()
    status_update_row = Gtk.Template.Child()
    status_update_progress_label = Gtk.Template.Child()
    tag_free = Gtk.Template.Child()
    tag_store_changes = Gtk.Template.Child()
    tag_spawned = Gtk.Template.Child()
    tag_in_use = Gtk.Template.Child()
    tag_updating = Gtk.Template.Child()
    tag_is_reserved = Gtk.Template.Child()
    tag_update_succeded = Gtk.Template.Child()
    tag_update_failed = Gtk.Template.Child()
    action_button_spawn = Gtk.Template.Child()
    action_button_unspawn = Gtk.Template.Child()
    action_button_update = Gtk.Template.Child()
    action_button_delete = Gtk.Template.Child()
    allow_binpkgs_checkbox = Gtk.Template.Child()
    applications_settings_group = Gtk.Template.Child()
    applications_container = Gtk.Template.Child()
    applications_actions_container = Gtk.Template.Child()
    applications_button_cancel = Gtk.Template.Child()
    applications_button_apply = Gtk.Template.Child()

    # --------------------------------------------------------------------------
    # Lifecycle:

    def __init__(self, toolset: Toolset, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__()
        self.toolset = toolset
        self.content_navigation_view = content_navigation_view

        self.apps_changed = False
        self.update_in_progress: ToolsetUpdate | None = None
        self.tools_selection: Dict[ToolsetApplication, bool] = {app: False for app in ToolsetApplication.ALL}
        self.tools_selection_versions: Dict[ToolsetApplication, ToolsetApplicationSelection] = {app: app.versions[0] for app in ToolsetApplication.ALL}
        self.tools_selection_patches: Dict[ToolsetApplication, list[Gio.File | str]] = {app: [] for app in ToolsetApplication.ALL}

        self._mount_action_group = Gio.SimpleActionGroup()
        self._add_mount_action("mount_read_only", self.mount_read_only)
        self._add_mount_action("mount_read_write", self.mount_read_write)
        self.insert_action_group("mount", self._mount_action_group)

        self._unmount_action_group = Gio.SimpleActionGroup()
        self.unmount_save_action = self._add_unmount_action("unmount_save", self.unmount_save)
        self._add_unmount_action("unmount_discard", self.unmount_discard)
        self.insert_action_group("unmount", self._unmount_action_group)

        self.setup_toolset_details()
        self.setup_runs_on()
        self.load_update_state()
        self.load_bindings()
        self.setup_status()
        self.connect("realize", self.on_realize)

        toolset.event_bus.subscribe(ToolsetEvents.SPAWNED_CHANGED, self.load_bindings)
        toolset.event_bus.subscribe(ToolsetEvents.SPAWNED_CHANGED, self.setup_status)
        toolset.event_bus.subscribe(ToolsetEvents.IN_USE_CHANGED, self.setup_status)
        toolset.event_bus.subscribe(ToolsetEvents.IS_RESERVED_CHANGED, self.setup_toolset_details)
        toolset.event_bus.subscribe(ToolsetEvents.IS_RESERVED_CHANGED, self.setup_status)
        MultiStageProcess.event_bus.subscribe(MultiStageProcessEvent.STARTED_PROCESSES_CHANGED, self.toolsets_updates_updated)

    def on_realize(self, widget):
        # Disables toolset_name_row auto focus on start
        self.get_root().set_focus(None)

    # --------------------------------------------------------------------------
    # Main details:

    def setup_toolset_details(self, event_data = None):
        """Displays main details of the toolset."""
        self.toolset_name_row.set_text(self.toolset.name)
        self.status_file_row.set_subtitle(self.toolset.file_path())
        self.status_size_row.set_subtitle(get_file_size_string(self.toolset.file_path()) or "unknown")
        source = self.toolset.metadata.get('source')
        timestamp_date_created = self.toolset.metadata.get('date_created')
        timestamp_date_updated = self.toolset.metadata.get('date_updated')
        allow_binpkgs = self.toolset.metadata.get('allow_binpkgs', False)
        date_created = datetime.fromtimestamp(timestamp_date_created) if isinstance(timestamp_date_created, int) else None
        date_updated = datetime.fromtimestamp(timestamp_date_updated) if isinstance(timestamp_date_updated, int) else None
        source_url = urlparse(source).path if source else None
        filename = os.path.basename(source_url) if source_url else None
        # Display source, date_created, date_updateds
        self.toolset_date_created_row.set_subtitle(date_created.strftime("%Y-%m-%d %H:%M") if date_created else "unknown")
        self.toolset_date_updated_row.set_subtitle(date_updated.strftime("%Y-%m-%d %H:%M") if date_updated else "unknown")
        self.toolset_source_row.set_subtitle(filename or "unknown")
        self.allow_binpkgs_checkbox.set_active(allow_binpkgs)
        if event_data is None or not self.toolset.is_reserved:
            self.load_initial_applications_selection()
            self.load_applications()

    def setup_runs_on(self):
        """Machine where toolset runs: this computer or virtual machine. Can be changed while toolset is not used."""
        current = self.toolset.machine
        self.runs_on_row.set_visible(virtual_machines_supported() or current is not None)
        # This computer is listed only on Linux (or when toolset still uses it, so it's displayed).
        local = sys.platform.startswith("linux") or current is None
        self.runs_on_machines = ([None] if local else []) + list(Repository.BuildMachine.value)
        fit_popup_to_items(self.runs_on_row)
        self.runs_on_row.set_model(Gtk.StringList.new([machine.name if machine else "This computer" for machine in self.runs_on_machines]))
        self.runs_on_row.set_selected(self.runs_on_machines.index(current) if current in self.runs_on_machines else 0)
        self.runs_on_row.connect("notify::selected", self.on_runs_on_changed)

    def on_runs_on_changed(self, row, param):
        machine = self.runs_on_machines[row.get_selected()]
        if machine is self.toolset.machine:
            return
        self.toolset.machine_id = machine.id if machine else None
        Repository.Toolset.save()

    def setup_status(self, _ = None):
        """Updates controls visibility and sensitivity for current status."""
        self.runs_on_row.set_sensitive(not self.toolset.spawned and not self.toolset.is_reserved)
        self.toolset_name_row.set_editable(
            not self.toolset.spawned
            and not self.toolset.is_reserved
        )
        self.toolset_name_row.set_sensitive(
            not self.toolset.spawned
            and not self.toolset.is_reserved
        )
        self.tag_free.set_visible(
            not self.toolset.spawned
            and not self.toolset.in_use
            and not self.toolset.is_reserved
        )
        self.tag_store_changes.set_visible(
            self.toolset.spawned
            and self.toolset.store_changes
        )
        self.tag_is_reserved.set_visible(self.toolset.is_reserved)
        self.tag_in_use.set_visible(self.toolset.in_use)
        self.tag_spawned.set_visible(self.toolset.spawned)
        self.tag_updating.set_visible(
            self.update_in_progress
            and self.update_in_progress.status == MultiStageProcessState.IN_PROGRESS
        )
        self.tag_update_succeded.set_visible(
            self.update_in_progress
            and self.update_in_progress.status == MultiStageProcessState.COMPLETED
        )
        self.tag_update_failed.set_visible(
            self.update_in_progress
            and self.update_in_progress.status == MultiStageProcessState.FAILED
        )
        self.action_button_spawn.set_sensitive(
            not self.toolset.spawned
            and not self.toolset.in_use
            and not self.toolset.is_reserved
        )
        self.action_button_spawn.set_visible(not self.toolset.spawned)
        self.action_button_unspawn.set_sensitive(
            self.toolset.spawned
            and not self.toolset.in_use
            and not self.toolset.is_reserved
        )
        self.action_button_unspawn.set_visible(self.toolset.spawned)
        self.action_button_update.set_sensitive(not self.toolset.spawned)
        self.action_button_delete.set_sensitive(
            not self.toolset.spawned
            and not self.toolset.in_use
            and not self.toolset.is_reserved
        )
        self.status_bindings_row.set_visible(self.toolset.spawned)
        self.status_update_row.set_visible(self.update_in_progress)
        self.status_update_row.set_subtitle(
            "" if not self.update_in_progress
            else "Update in progress" if self.update_in_progress.status == MultiStageProcessState.IN_PROGRESS
            else "Update completed" if self.update_in_progress.status == MultiStageProcessState.COMPLETED
            else "Update failed" if self.update_in_progress.status == MultiStageProcessState.FAILED
            else ""
        )
        self.status_update_progress_label.set_visible(
            self.update_in_progress
            and self.update_in_progress.status == MultiStageProcessState.IN_PROGRESS
        )
        self.applications_container.set_sensitive(not self.toolset.spawned)
        self.applications_settings_group.set_sensitive(not self.toolset.spawned)
        self.applications_button_apply.set_sensitive(
            not self.toolset.in_use and not self.toolset.is_reserved
            and (
                not self.update_in_progress
                or self.update_in_progress.status == MultiStageProcessState.COMPLETED
                or self.update_in_progress.status == MultiStageProcessState.FAILED
            )
        )
        self.applications_actions_container.set_visible(self.apps_changed)
        self.unmount_save_action.set_enabled(self.toolset.store_changes)

    def load_bindings(self, _ = None):
        """Loads toolset bindings rows."""
        if hasattr(self, "_binding_rows"):
            for row in self._binding_rows:
                self.status_bindings_row.remove(row)
        self.status_bindings_row.set_expanded(False)
        self._binding_rows = []
        if self.toolset.current_bindings:
            for binding in self.toolset.current_bindings:
                row = Adw.ActionRow(title=binding.mount_path)
                access_str = "RW" if binding.store_changes else "RO"
                source_str = f"(Toolset){binding.toolset_path} ({access_str})" if binding.toolset_path else f"(Host){binding.host_path} ({access_str})" if binding.host_path else "(Temp)"
                source_label = Gtk.Label(label=source_str)
                source_label.get_style_context().add_class("dimmed")
                row.add_suffix(source_label)
                self.status_bindings_row.add_row(row)
                self._binding_rows.append(row)

    def load_update_state(self, started_processes: list[ToolsetUpdate] | None = None):
        if started_processes is None:
            started_processes = MultiStageProcess.get_started_processes_by_class(ToolsetUpdate)
        if self.update_in_progress is not None :
            self.update_in_progress.event_bus.unsubscribe(MultiStageProcessEvent.STATE_CHANGED, self)
        self.update_in_progress = next((process for process in started_processes if process.toolset == self.toolset), None)
        if self.update_in_progress:
            self.update_in_progress.event_bus.subscribe(
                MultiStageProcessEvent.STATE_CHANGED,
                self.toolsets_update_process_state_changed,
                self
            )
            self.status_update_progress_label.set_label(f"{int(self.update_in_progress.progress * 100)}%")
            self.update_in_progress.event_bus.subscribe(
                MultiStageProcessEvent.PROGRESS_CHANGED,
                self.toolsets_update_process_progress_changed,
                self
            )

    def toolsets_updates_updated(self, process_class: type[MultiStageProcess], started_processes: list[MultiStageProcess]):
        if issubclass(process_class, ToolsetUpdate):
            self.load_update_state(started_processes=started_processes)
            self.setup_status()

    def toolsets_update_process_state_changed(self, state: MultiStageProcessState):
        self.setup_status()

    def toolsets_update_process_progress_changed(self, progress: float):
        self.status_update_progress_label.set_label(f"{int(progress * 100)}%")

    @Gtk.Template.Callback()
    def status_update_row_clicked(self, sender):
        self.show_update(update=self.update_in_progress)

    # --------------------------------------------------------------------------
    # Changing toolset name:

    @Gtk.Template.Callback()
    def on_toolset_name_activate(self, sender):
        new_name = self.toolset_name_row.get_text()
        if new_name == self.toolset.name:
            self.get_root().set_focus(None)
            return
        is_name_available = ToolsetManager.shared().is_name_available(name=new_name)
        try:
            if not is_name_available:
                raise RuntimeError(f"Toolset name {new_name} is not available")
            ToolsetManager.shared().rename_toolset(toolset=self.toolset, name=new_name)
            self.get_root().set_focus(None)
            self.setup_toolset_details()
        except Exception as e:
            print(f"Error renaming toolset: {e}")
            self.toolset_name_row.add_css_class("error")
            self.toolset_name_row.grab_focus()

    @Gtk.Template.Callback()
    def on_toolset_name_changed(self, sender):
        is_name_available = ToolsetManager.shared().is_name_available(name=self.toolset_name_row.get_text()) or self.toolset_name_row.get_text() == self.toolset.name
        self.name_used_row.set_visible(not is_name_available)
        self.toolset_name_row.remove_css_class("error")

    # --------------------------------------------------------------------------
    # Apps selection:

    def load_initial_applications_selection(self):
        """Load initial selection of apps details."""
        self.apps_changed = False
        self.tools_selection.clear()
        self.tools_selection_versions.clear()
        self.tools_selection_patches.clear()
        for app in ToolsetApplication.ALL:
            app_install = self.toolset.get_app_install(app=app)
            self.tools_selection[app] = app_install is not None
            self.tools_selection_versions[app] = app_install.variant if app_install else app.versions[0]
            self.tools_selection_patches[app] = app_install.patches[:] if app_install else []

    def load_applications(self):
        """Prepares applications rows."""
        if hasattr(self, "_apps_rows"):
            for row in self._apps_rows:
                self.applications_group.remove(row)
        self._apps_rows = []
        for app in ToolsetApplication.ALL:
            app_install = self.toolset.get_app_install(app=app)
            if app.auto_select:
                continue
            if self.tools_selection[app]:
                subtitle = f"{app_install.variant.name}: {app_install.version}, Patched" if app_install.patches else f"{app_install.variant.name}: {app_install.version}"
            else:
                subtitle = "Not installed"
            row = Adw.ExpanderRow(title=app.name, subtitle=subtitle)
            row.set_enable_expansion(self.tools_selection[app])

            check_button = Gtk.CheckButton()
            check_button.set_active(self.tools_selection[app])
            check_button.connect("toggled", self._on_tool_selected, app, row)
            row.check_button = check_button
            row.add_prefix(check_button)

            versions_row = Adw.ActionRow()
            versions_row.set_activatable(False)

            toggle_group = CLToggleGroup()
            toggle_group.add_css_class("round")
            toggle_group.add_css_class("caption")
            for version in app.versions:
                toggle = CLToggle(label=version.name)
                toggle_group.add(toggle)
            if app_install: # Activate selected app variant toggle
                for i, version in enumerate(app.versions):
                    if version == app_install.variant:
                        toggle_group.set_active(i)
                        break
            def on_toggle_clicked(group, pspec, app, row):
                index = group.get_active()
                self.tools_selection_versions[app] = app.versions[index]
                self.apps_changed = True
                self.setup_status()
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

            for patch_filename in self.tools_selection_patches[app]:
                self._add_patch_file_row(app, row, patch_filename)

            self.applications_group.add(row)
            self._apps_rows.append(row)

    def _add_patch_file_row(self, app, app_row, file: Gio.File | str):
        if isinstance(file, Gio.File):
            patch_row = Adw.ActionRow(title=file.get_basename(), subtitle="Patch file", icon_name="copy-svgrepo-com-symbolic")
        else:
            patch_row = Adw.ActionRow(title=file, subtitle="Patch file", icon_name="copy-svgrepo-com-symbolic")
        remove_button = Gtk.Button()
        remove_button_content = Adw.ButtonContent(label="Remove", icon_name="error-box-svgrepo-com-symbolic")
        remove_button.set_child(remove_button_content)
        remove_button.get_style_context().add_class("destructive-action")
        remove_button.get_style_context().add_class("flat")
        remove_button.get_style_context().add_class("caption")
        remove_button.connect("clicked", self._delete_patch_pressed, app, app_row, patch_row)
        remove_button.file = file
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        wrapper.append(remove_button)
        patch_row.add_suffix(wrapper)
        app_row.add_row(patch_row)

    def _on_tool_selected(self, button: Gtk.CheckButton, app: ToolsetApplication, row: Adw.ExpanderRow):
        """Application checkbox toggled."""
        self.tools_selection[app] = button.get_active()
        row.set_enable_expansion(self.tools_selection[app])
        row.set_expanded(False)
        self.apps_changed = True
        self.setup_status()

    def _delete_patch_pressed(self, remove_button, app, app_row, patch_row):
        self.tools_selection_patches[app].remove(remove_button.file)
        app_row.remove(patch_row)
        self.apps_changed = True
        self.setup_status()

    def _on_add_patch_clicked(self, add_patch_row, app, app_row):
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
                remove_button.connect("clicked", self._delete_patch_pressed, app, app_row, patch_row)
                remove_button.file = file
                wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
                wrapper.append(remove_button)
                patch_row.add_suffix(wrapper)
                app_row.add_row(patch_row)
                self.apps_changed = True
                self.setup_status()
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

    @Gtk.Template.Callback()
    def on_allow_binpkgs_toggled(self, checkbox):
        self.toolset.metadata['allow_binpkgs'] = checkbox.get_active()
        Repository.Toolset.save()

    @Gtk.Template.Callback()
    def applications_button_cancel_clicked(self, sender):
        self.load_initial_applications_selection()
        self.load_applications()
        self.apps_changed = False
        self.setup_status()

    @Gtk.Template.Callback()
    def applications_button_apply_clicked(self, sender):
        def update(authorization_keeper: AuthorizationKeeper):
            if authorization_keeper:
                apps_selection = [
                    ToolsetApplicationSelection(
                        app=app,
                        version=self.tools_selection_versions[app],
                        selected=self.tools_selection[app],
                        patches=self.tools_selection_patches[app]
                    )
                    for app, _ in self.tools_selection.items()
                ]
                self.start_update(authorization_keeper=authorization_keeper, update_packages=False, apps_selection=apps_selection)
        authorize_toolset_action(callback=update, machine=self.toolset.machine)

    def start_update(self, authorization_keeper: AuthorizationKeeper, update_packages: bool = True, apps_selection: list[ToolsetApplicationSelection] | None = None):
        if self.update_in_progress:
            self.update_in_progress.clean_from_started_processes()
        allow_binpkgs = self.toolset.metadata.get('allow_binpkgs', False)
        update = ToolsetUpdate(toolset=self.toolset, allow_binpkgs=allow_binpkgs, update_packages=update_packages, apps_selection=apps_selection)
        update.start(authorization_keeper=authorization_keeper)
        self.show_update(update=update)

    def show_update(self, update: ToolsetUpdate):
        update_view = MultistageProcessExecutionView()
        update_view.set_multistage_process(multistage_process=update)
        self.content_navigation_view.push_view(update_view, title="Updating toolset")

    # --------------------------------------------------------------------------
    # Toolset actions:

    @Gtk.Template.Callback()
    def action_button_spawn_clicked(self, sender):
        self.spawn(store_changes=False)

    @Gtk.Template.Callback()
    def action_button_unspawn_clicked(self, sender):
        self.unspawn(store_changes=True)

    @Gtk.Template.Callback()
    def action_button_update_clicked(self, sender):
        def update(authorization_keeper: AuthorizationKeeper):
            if authorization_keeper:
                self.start_update(authorization_keeper=authorization_keeper, update_packages=True)
        authorize_toolset_action(callback=update, machine=self.toolset.machine)

    @Gtk.Template.Callback()
    def action_button_delete_clicked(self, sender):
        ToolsetManager.shared().remove_toolset(toolset=self.toolset)
        if hasattr(self, "_window"):
            self._window.close()
        elif hasattr(self, "content_navigation_view"):
            self.content_navigation_view.pop()

    def _add_mount_action(self, name, callback) -> Gio.SimpleAction:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self._mount_action_group.add_action(action)
        return action

    def _add_unmount_action(self, name, callback) -> Gio.SimpleAction:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self._unmount_action_group.add_action(action)
        return action

    def mount_read_only(self, action, param):
        self.spawn(store_changes=False)

    def mount_read_write(self, action, param):
        self.spawn(store_changes=True)

    def unmount_save(self, action, param):
        self.unspawn(store_changes=True)

    def unmount_discard(self, action, param):
        self.unspawn(store_changes=False)

    def spawn(self, store_changes: bool):
        def spawn(authorization_keeper: AuthorizationKeeper):
            if authorization_keeper:
                try:
                    self.toolset.reserve()
                    self.toolset.spawn(store_changes=store_changes)
                    self.toolset.analyze()
                except Exception as e:
                    print(e)
                finally:
                    self.toolset.release()
        authorize_toolset_action(callback=spawn, machine=self.toolset.machine)

    def unspawn(self, store_changes: bool):
        def unspawn(authorization_keeper: AuthorizationKeeper):
            if authorization_keeper:
                try:
                    self.toolset.reserve()
                    self.toolset.unspawn(rebuild_squashfs_if_needed=store_changes)
                except Exception as e:
                    print(e)
                finally:
                    self.toolset.release()
        authorize_toolset_action(callback=unspawn, machine=self.toolset.machine)

