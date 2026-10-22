import os
import threading

from gi.repository import Gtk, Adw, GLib, Gio

from .app_section import app_section
from .library_storage import scan_library_storage, remove_library_item
from .repository import Repository, RepositoryEvent, library_directory
from .multistage_process import MultiStageProcess, MultiStageProcessEvent, MultiStageProcessState
from .event_bus import SharedEvent
from .toolset import ToolsetEvents
from .settings import SettingsEvents
from .snapshot_manager import SnapshotManager
from .formatters import format_size
from .cl_toggle_group import CLToggle, CLToggleGroup
from .runner import HOST_RUNNER
from .status_indicator import StatusIndicator
from .repository import collapse_home
from .library import LibraryChange, library_target, change_problem
from .settings_section import LibraryChangeDialog, items_using_library


@app_section(title="Library", icon="drive-harddisk-symbolic", order=8_500)
class LibrarySection(Gtk.Box):
    """Storage overview and safe cleanup for the selected Catalyst Lab library."""
    __gtype_name__ = "LibrarySection"

    def __init__(self, content_navigation_view: Adw.NavigationView, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, **kwargs)
        self.content_navigation_view = content_navigation_view
        self.library_path = os.path.realpath(os.path.expanduser(library_directory()))
        self._generation = 0
        self._scanning = False
        self._delete_buttons: list[tuple[Gtk.Widget, str, dict]] = []
        self._last_snapshot = None
        self._safety_reason = None
        self._observing_safety_events = False
        self._observed_machines = set()
        self._observed_toolsets = set()
        self._vm_runner_rows = {}

        # Show the selected Library location on the Overview page.
        self.library_location_row = Adw.ActionRow(title="Location", subtitle=self.library_path,
                                                   subtitle_selectable=True)
        self.library_location_row.add_css_class("property")
        self.library_size_label = Gtk.Label(label="Scanning…", valign=Gtk.Align.CENTER)
        self.library_size_label.add_css_class("numeric")
        self.library_location_row.add_suffix(self.library_size_label)
        open_button = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Open folder",
                                 valign=Gtk.Align.CENTER)
        open_button.add_css_class("flat")
        open_button.connect("clicked", self._open_library_folder)
        self.library_location_row.add_suffix(open_button)
        change_button = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        change_button.connect("clicked", self._change_library_location)
        self.library_location_row.add_suffix(change_button)
        location_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        location_list.add_css_class("boxed-list")
        location_list.append(self.library_location_row)
        location_list.set_margin_top(12)
        location_list.set_margin_bottom(12)
        self.page_names = ("overview", "cache", "binpkgs", "snapshots", "projects", "builds")
        self.page_toggle = CLToggleGroup(valign=Gtk.Align.CENTER)
        self.page_toggle.add_css_class("round")
        self.page_toggle.add_css_class("caption")
        for title in ("Overview", "Cache", "Packages", "Snapshots", "Projects", "Builds"):
            self.page_toggle.add(CLToggle(label=title))
        self.page_toggle.set_active(0)
        self.page_toggle.connect("notify::active", self._on_page_toggle)
        self.stack = Adw.ViewStack(vexpand=True)
        self.stack.connect("notify::visible-child-name", self._on_visible_page_changed)
        self.append(self.stack)

        self.overview_group = Adw.PreferencesGroup(title="Library storage")
        self._group_rows = {self.overview_group: []}
        _overview_header, overview_box = self._page_box("Overview")
        overview_box.prepend(location_list)
        self.overview_status = Gtk.Label(xalign=0, wrap=True)
        self.overview_status.add_css_class("dimmed")
        self.overview_status.set_margin_top(8)
        overview_box.prepend(self.overview_status)
        overview_box.append(self.overview_group)
        self.runner_group = Adw.PreferencesGroup(title="Runners", description="Execution endpoints available to operations.")
        self.host_runner_row = Adw.ActionRow(title="Host")
        self._update_host_runner_row()
        self.host_runner_row.set_sensitive(HOST_RUNNER.supported())
        self.host_runner_row.add_prefix(Gtk.Image.new_from_icon_name("computer-symbolic"))
        self.runner_group.add(self.host_runner_row)
        HOST_RUNNER.event_bus.subscribe(SharedEvent.STATE_UPDATED, self._on_host_runner_updated)
        self._refresh_vm_runner_rows()
        overview_box.append(self.runner_group)

        _cache_header, self.cache_container = self._page_box("Cache")
        self.cache_safety = Gtk.Label(xalign=0, wrap=True)
        self.cache_safety.add_css_class("dimmed")
        self.cache_safety.set_margin_top(8)
        self.cache_container.prepend(self.cache_safety)
        self._cache_groups = []

        _binpkgs_header, self.binpkgs_container = self._page_box("Packages")
        self.binpkgs_safety = Gtk.Label(xalign=0, wrap=True)
        self.binpkgs_safety.add_css_class("dimmed")
        self.binpkgs_safety.set_margin_top(8)
        self.binpkgs_container.prepend(self.binpkgs_safety)
        self._binpkgs_groups = []

        _snapshots_header, self.snapshots_container = self._page_box("Snapshots")
        self.snapshots_safety = Gtk.Label(xalign=0, wrap=True)
        self.snapshots_safety.add_css_class("dimmed")
        self.snapshots_safety.set_margin_top(8)
        self.snapshots_container.prepend(self.snapshots_safety)
        self._snapshot_groups = []
        self._build_snapshot_auto_clean_option()

        _projects_header, self.projects_container = self._page_box("Projects")
        self.projects_safety = Gtk.Label(xalign=0, wrap=True)
        self.projects_safety.add_css_class("dimmed")
        self.projects_safety.set_margin_top(8)
        self.projects_container.prepend(self.projects_safety)
        self._project_groups = []

        _builds_header, self.builds_container = self._page_box("Builds")
        self.builds_safety = Gtk.Label(xalign=0, wrap=True)
        self.builds_safety.add_css_class("dimmed")
        self.builds_safety.set_margin_top(8)
        self.builds_container.prepend(self.builds_safety)
        self._build_groups = []
        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)
        self.refresh()

    def _update_host_runner_row(self):
        subtitle = ("Runs tools on this Linux computer" if HOST_RUNNER.supported()
                    else "Unavailable on this platform; macOS uses VM runners")
        if HOST_RUNNER.supported() and HOST_RUNNER.users:
            subtitle += " · In use by: " + ", ".join(HOST_RUNNER.users)
        self.host_runner_row.set_subtitle(subtitle)

    def _open_library_folder(self, *_args):
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(self.library_path).get_uri(), None)

    def _change_library_location(self, *_args):
        if items := items_using_library():
            self._show_library_message("Library Is in Use", "Stop these and try again:\n\n" +
                                       "\n".join(f"• {item}" for item in items))
            return
        dialog = Gtk.FileDialog(title="Choose Library Folder", modal=True)
        dialog.set_initial_folder(Gio.File.new_for_path(os.path.dirname(library_directory())))
        dialog.select_folder(self.get_root(), None, self._on_library_folder_selected)

    def _on_library_folder_selected(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return # Canceled.
        if folder is None or folder.get_path() is None:
            return
        try:
            target = library_target(folder.get_path())
        except ValueError as error:
            self._show_library_message("Can't Use This Folder", str(error))
            return

        location = collapse_home(target.path)
        if target.existing:
            dialog = Adw.AlertDialog(heading="Switch Library?", body=(
                f"{location} contains a library. Catalyst Lab will restart and use it.\n\nCurrent library stays in "
                f"{collapse_home(library_directory())}."))
            dialog.add_response("cancel", "Cancel")
            dialog.add_response(LibraryChange.SWITCH.value, "Switch and Restart")
            dialog.set_response_appearance(LibraryChange.SWITCH.value, Adw.ResponseAppearance.SUGGESTED)
        else:
            dialog = Adw.AlertDialog(heading="Change Library Location?", body=(
                f"Library will be kept in {location}. Move or copy the current library there, or create a new empty "
                "library. Catalyst Lab restarts after the change."))
            dialog.add_response("cancel", "Cancel")
            dialog.add_response(LibraryChange.NEW.value, "Create New")
            dialog.add_response(LibraryChange.COPY.value, "Copy")
            dialog.add_response(LibraryChange.MOVE.value, "Move")
            dialog.set_response_appearance(LibraryChange.MOVE.value, Adw.ResponseAppearance.SUGGESTED)
            dialog.set_prefer_wide_layout(True)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda dialog, response: self._confirm_library_change(target, response))
        dialog.present(self)

    def _confirm_library_change(self, target, response: str):
        if response == "cancel":
            return
        change = LibraryChange(response)
        if items := items_using_library():
            self._show_library_message("Library Is in Use", "Stop these and try again:\n\n" +
                                       "\n".join(f"• {item}" for item in items))
            return
        if problem := change_problem(target, change):
            self._show_library_message("Can't Change Library", problem)
            return
        LibraryChangeDialog(target, change).present(self)

    def _show_library_message(self, heading: str, body: str):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def _on_host_runner_updated(self, *_args):
        self._update_host_runner_row()

    def _refresh_vm_runner_rows(self):
        for row, _status in self._vm_runner_rows.values():
            self.runner_group.remove(row)
        self._vm_runner_rows.clear()
        for runner in sorted(Repository.Runner.value, key=lambda item: item.name.lower()):
            row = Adw.ActionRow(title=runner.name, subtitle=runner.short_details)
            row.add_prefix(Gtk.Image.new_from_icon_name("virtual-machine-symbolic"))
            status = StatusIndicator()
            status.set_values(runner.status_indicator_values)
            row.add_suffix(status)
            self.runner_group.add(row)
            self._vm_runner_rows[runner] = (row, status)

    def _on_vm_runner_updated(self, runner, *_args):
        if rows := self._vm_runner_rows.get(runner):
            row, status = rows
            row.set_subtitle(runner.short_details)
            status.set_values(runner.status_indicator_values)
        self._refresh_safety()

    def header_bar_end_widgets(self) -> list[Gtk.Widget]:
        """Use the same rounded toolbar toggles as the other multi-view screens."""
        return [self.page_toggle]

    def _on_page_toggle(self, toggle_group, _param):
        index = toggle_group.get_active()
        if 0 <= index < len(self.page_names):
            self.stack.set_visible_child_name(self.page_names[index])

    def _on_visible_page_changed(self, stack, _param):
        name = stack.get_visible_child_name()
        if name in self.page_names:
            self.page_toggle.set_active(self.page_names.index(name))

    def _build_snapshot_auto_clean_option(self):
        option_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        option_list.add_css_class("boxed-list")
        self.auto_clean_snapshots_row = Adw.SwitchRow(title="Auto clean unused snapshots")
        self.auto_clean_snapshots_row.set_active(Repository.Settings.value.auto_clean_snapshots)
        self.auto_clean_snapshots_row.connect("notify::active", self._on_auto_clean_snapshots_toggled)
        option_list.append(self.auto_clean_snapshots_row)
        self.snapshots_container.append(option_list)

    def _on_auto_clean_snapshots_toggled(self, row, _param):
        enabled = row.get_active()
        if enabled and SnapshotManager.shared().unused_snapshots():
            self._confirm_auto_clean_snapshots(row)
            return
        Repository.Settings.value.auto_clean_snapshots = enabled

    def _confirm_auto_clean_snapshots(self, row):
        snapshots = SnapshotManager.shared().unused_snapshots()
        names = "\n".join(f"{snapshot.name} ({snapshot.short_details})" for snapshot in snapshots)
        dialog = Adw.AlertDialog(
            heading="Remove unused snapshots?",
            body=("These snapshots are not selected in project configuration, are not the newest, and are not "
                  "currently in use. They will be removed even if referenced by previous builds or added from file:\n\n"
                  f"{names}"),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("enable", "Remove and enable")
        dialog.set_response_appearance("enable", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def on_response(_dialog, response):
            if response == "enable":
                Repository.Settings.value.auto_clean_snapshots = True
            else:
                row.handler_block_by_func(self._on_auto_clean_snapshots_toggled)
                row.set_active(False)
                row.handler_unblock_by_func(self._on_auto_clean_snapshots_toggled)

        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    def _on_realize(self, *_args):
        if self._observing_safety_events:
            return
        self._observing_safety_events = True
        MultiStageProcess.event_bus.subscribe(
            MultiStageProcessEvent.STARTED_PROCESSES_CHANGED, self._on_safety_event, self
        )
        from .qemu_run import QemuRun, QemuRunEvent
        self._qemu_run_event_bus = QemuRun.running_runs_event_bus
        self._qemu_run_changed_event = QemuRunEvent.RUNNING_RUNS_CHANGED
        self._qemu_run_event_bus.subscribe(self._qemu_run_changed_event, self._on_safety_event, self)
        for repository in (Repository.Runner, Repository.Toolset):
            repository.event_bus.subscribe(RepositoryEvent.VALUE_CHANGED, self._on_resources_changed, self)
        Repository.Settings.value.event_bus.subscribe(
            SettingsEvents.AUTO_CLEAN_SNAPSHOTS_CHANGED, self._on_auto_clean_setting_changed, self
        )
        self._on_auto_clean_setting_changed()
        self._sync_resource_event_subscriptions()
        self._refresh_safety()

    def _on_unrealize(self, *_args):
        if not self._observing_safety_events:
            return
        self._observing_safety_events = False
        MultiStageProcess.event_bus.unsubscribe(MultiStageProcessEvent.STARTED_PROCESSES_CHANGED, self)
        self._qemu_run_event_bus.unsubscribe(self._qemu_run_changed_event, self)
        for repository in (Repository.Runner, Repository.Toolset):
            repository.event_bus.unsubscribe(RepositoryEvent.VALUE_CHANGED, self)
        Repository.Settings.value.event_bus.unsubscribe(SettingsEvents.AUTO_CLEAN_SNAPSHOTS_CHANGED, self)
        for machine in self._observed_machines:
            machine.event_bus.unsubscribe(SharedEvent.STATE_UPDATED, self)
        for toolset in self._observed_toolsets:
            for event in (ToolsetEvents.SPAWNED_CHANGED, ToolsetEvents.IN_USE_CHANGED,
                          ToolsetEvents.IS_RESERVED_CHANGED):
                toolset.event_bus.unsubscribe(event, self)
        self._observed_machines.clear()
        self._observed_toolsets.clear()

    def _sync_resource_event_subscriptions(self):
        if not self._observing_safety_events:
            return
        machines = set(Repository.Runner.value)
        toolsets = set(Repository.Toolset.value)
        for machine in self._observed_machines - machines:
            machine.event_bus.unsubscribe(SharedEvent.STATE_UPDATED, self)
        for machine in machines - self._observed_machines:
            machine.event_bus.subscribe(SharedEvent.STATE_UPDATED, self._on_vm_runner_updated, self)
        toolset_events = (ToolsetEvents.SPAWNED_CHANGED, ToolsetEvents.IN_USE_CHANGED,
                          ToolsetEvents.IS_RESERVED_CHANGED)
        for toolset in self._observed_toolsets - toolsets:
            for event in toolset_events:
                toolset.event_bus.unsubscribe(event, self)
        for toolset in toolsets - self._observed_toolsets:
            for event in toolset_events:
                toolset.event_bus.subscribe(event, self._on_safety_event, self)
        self._observed_machines = machines
        self._observed_toolsets = toolsets

    def _on_resources_changed(self, *_args):
        if not self._observing_safety_events:
            return
        self._refresh_vm_runner_rows()
        self._sync_resource_event_subscriptions()
        self._refresh_safety()

    def _on_safety_event(self, *_args):
        if self._observing_safety_events:
            self._refresh_safety()

    def _on_auto_clean_setting_changed(self, *_args):
        active = Repository.Settings.value.auto_clean_snapshots
        if self.auto_clean_snapshots_row.get_active() == active:
            return
        self.auto_clean_snapshots_row.handler_block_by_func(self._on_auto_clean_snapshots_toggled)
        self.auto_clean_snapshots_row.set_active(active)
        self.auto_clean_snapshots_row.handler_unblock_by_func(self._on_auto_clean_snapshots_toggled)

    def _page_box(self, title: str) -> tuple[Gtk.Box, Gtk.Box]:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0,
                         margin_start=24, margin_end=24)
        page.append(header)
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20,
                          margin_start=24, margin_end=24, margin_bottom=24)
        scroller.set_child(content)
        page.append(scroller)
        name = {"Overview": "overview", "Cache": "cache", "Packages": "binpkgs",
                "Snapshots": "snapshots", "Projects": "projects", "Builds": "builds"}[title]
        self.stack.add_titled(page, name, title)
        return header, content

    def refresh(self):
        if self._scanning:
            return
        self._scanning = True
        self.overview_status.set_text(f"Scanning {self.library_path}…")
        for button, _section, _item in self._delete_buttons:
            button.set_sensitive(False)
        self._generation += 1
        generation = self._generation

        def scan():
            result = scan_library_storage(self.library_path)
            GLib.idle_add(self._show_snapshot, generation, result)

        threading.Thread(target=scan, name="library-storage-scan", daemon=True).start()

    def _show_snapshot(self, generation: int, snapshot: dict):
        if generation != self._generation:
            return GLib.SOURCE_REMOVE
        self._scanning = False
        self._last_snapshot = snapshot
        if snapshot.get("error"):
            self.overview_status.set_text(f"Could not scan the library: {snapshot['error']}")
            self.library_size_label.set_text("Size unavailable")
            self._refresh_safety()
            return GLib.SOURCE_REMOVE

        self.library_size_label.set_text(format_size(snapshot["total_size"]))
        self.overview_status.set_text(
            "Library storage breakdown"
        )
        self._replace_children(self.overview_group)
        for section in snapshot["sections"]:
            self._add_group_row(self.overview_group, self._storage_row(section, snapshot["total_size"]))

        self._delete_buttons = []
        self._populate_cleanup_groups(self.cache_container, self._cache_groups, snapshot["cache_items"], "Cache")
        self._populate_cleanup_groups(
            self.binpkgs_container, self._binpkgs_groups, snapshot["binpkg_items"], "Packages"
        )
        self._populate_cleanup_groups(
            self.snapshots_container, self._snapshot_groups, snapshot["snapshot_items"], "Snapshots"
        )
        self._populate_cleanup_groups(
            self.projects_container, self._project_groups, snapshot["project_items"], "Projects"
        )
        self._populate_build_groups(self.builds_container, self._build_groups,
                                    snapshot.get("build_items", []))
        self._refresh_safety()
        return GLib.SOURCE_REMOVE

    def _replace_children(self, group):
        for child in self._group_rows[group]:
            group.remove(child)
        self._group_rows[group] = []

    def _add_group_row(self, group, row):
        group.add(row)
        self._group_rows[group].append(row)

    def _storage_row(self, section: dict, total_size: int):
        row = Adw.ActionRow(title=section["name"], subtitle=section["description"])
        row.set_tooltip_text(section["path"])
        suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, valign=Gtk.Align.CENTER)
        progress = Gtk.ProgressBar(show_text=False, width_request=96)
        progress.set_fraction(section["size"] / total_size if total_size else 0.0)
        progress.set_tooltip_text(f"{section['size'] / total_size:.1%} of library" if total_size else "Empty")
        size_label = Gtk.Label(label=format_size(section["size"]), width_chars=9, xalign=1)
        size_label.add_css_class("numeric")
        suffix.append(progress)
        suffix.append(size_label)
        row.add_suffix(suffix)
        return row

    def _populate_cleanup_groups(self, container, groups: list, items: list[dict], section: str):
        for group in groups:
            container.remove(group)
        groups.clear()
        if not items:
            group = Adw.PreferencesGroup(
                title=f"No recognized {section.lower()} data",
                description=f"No known Catalyst Lab {section.lower()} object types were found.",
            )
            container.append(group)
            groups.append(group)
            return
        by_type = {}
        for item in items:
            group = by_type.get(item["category"])
            if group is None:
                group = Adw.PreferencesGroup(title=item["type_title"], description=item["description"])
                by_type[item["category"]] = group
                container.append(group)
                groups.append(group)
            row = Adw.ExpanderRow(title=item["name"],
                                subtitle=f"{format_size(item['size'])} · {item['details']}")
            row.set_tooltip_text(item["path"])
            row.add_prefix(Gtk.Image.new_from_icon_name(
                "folder-symbolic" if item["is_dir"] else "text-x-generic-symbolic"
            ))
            delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=f"Delete {section}/{item['name']}")
            delete.add_css_class("destructive-action")
            delete.set_valign(Gtk.Align.CENTER)
            delete.connect("clicked", self._confirm_delete, section, item)
            row.add_suffix(delete)
            detail_rows = [("Purpose", item["description"]), ("Used by", item["usage"]),
                           ("Location", item["path"])]
            for metadata in item.get("metadata", []):
                if isinstance(metadata, (tuple, list)) and len(metadata) == 2:
                    detail_rows.append((str(metadata[0]), str(metadata[1])))
                elif isinstance(metadata, str) and ":" in metadata:
                    title, detail = metadata.split(":", 1)
                    detail_rows.append((title, detail.strip()))
                elif isinstance(metadata, str):
                    title = "Last modified" if metadata.startswith("Modified ") else "Additional information"
                    detail_rows.append((title, metadata.removeprefix("Modified ")))
            for title, detail in detail_rows:
                detail_row = Adw.ActionRow(title=title, subtitle=detail)
                detail_row.set_subtitle_selectable(True)
                row.add_row(detail_row)
            group.add(row)
            self._delete_buttons.append((delete, section, item))

    def _populate_build_groups(self, container, groups: list, items: list[dict]):
        for group in groups:
            container.remove(group)
        groups.clear()
        if not items:
            group = Adw.PreferencesGroup(title="No recognized builds",
                                         description="No saved stage build records were found.")
            container.append(group)
            groups.append(group)
            return
        by_project = {}
        for item in items:
            project_key = item.get("project_key", "")
            group = by_project.get(project_key)
            if group is None:
                project_name = item.get("project_name") or project_key or "Unknown project"
                group = Adw.PreferencesGroup(title=project_name,
                                             description="Stage builds and their logs for this project.")
                by_project[project_key] = group
                container.append(group)
                groups.append(group)
            row = Adw.ExpanderRow(title=item["name"],
                                  subtitle=f"{format_size(item['size'])} · {item['details']}")
            row.set_tooltip_text(item["path"])
            row.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic"))
            delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=f"Delete build {item['name']}")
            delete.add_css_class("destructive-action")
            delete.set_valign(Gtk.Align.CENTER)
            delete.connect("clicked", self._confirm_delete, "Builds", item)
            row.add_suffix(delete)
            details = [("Project", item.get("project_name") or project_key),
                       ("Purpose", item["description"]), ("Location", item["path"])]
            for metadata in item.get("metadata", []):
                if isinstance(metadata, str) and ":" in metadata:
                    title, value = metadata.split(":", 1)
                    details.append((title, value.strip()))
            for title, value in details:
                detail_row = Adw.ActionRow(title=title, subtitle=value)
                detail_row.set_subtitle_selectable(True)
                row.add_row(detail_row)
            group.add(row)
            self._delete_buttons.append((delete, "Builds", item))

    @staticmethod
    def _same_or_inside(path: str | None, item_path: str | None) -> bool:
        if not path or not item_path:
            return False
        try:
            path = os.path.realpath(path)
            item_path = os.path.realpath(item_path)
            return os.path.commonpath((path, item_path)) == item_path
        except (OSError, ValueError):
            return False

    @staticmethod
    def _item_use_reason(library_path: str, section: str, item: dict) -> str | None:
        """Return a blocker only when active app state points at this exact library object."""
        target = os.path.realpath(item.get("path", ""))
        category = item.get("category")
        relative = item.get("relative_path", "").replace(os.sep, "/")
        active = [process for process in MultiStageProcess.started_processes
                  if process.status == MultiStageProcessState.IN_PROGRESS]

        def project_for(process):
            return (getattr(process, "project_directory", None)
                    or getattr(process, "project", None))

        def same_path(path):
            return bool(path) and os.path.realpath(path) == target

        if section == "Temporary" and category == "Runners":
            for runner in Repository.Runner.value:
                if runner.instance_name == os.path.basename(target) and (runner.is_used or runner._workspace_users > 0):
                    return f"VM runner {runner.name} is using this workspace"

        for process in active:
            project = project_for(process)
            project_id = str(getattr(project, "id", ""))
            project_key = project_id.replace("-", "")
            if section == "Projects" and item.get("project_id") == project_id:
                return f"project {item['name']} is being used by {process.title}"
            if section == "Builds" and item.get("project_key") == project_key:
                if LibrarySection._same_or_inside(getattr(getattr(process, "build", None), "path", None), target):
                    return f"this build is active in {process.title}"
                if any(LibrarySection._same_or_inside(getattr(getattr(stage, "build", None), "path", None), target)
                       for stage in getattr(process, "stages", [])):
                    return f"this build is active in {process.title}"
                if item.get("timestamp") == getattr(process, "timestamp", None):
                    return f"this build is active in {process.title}"

            if section == "Packages" and project is not None:
                try:
                    package_dir = project.get_packages_directory()
                    if package_dir and same_path(package_dir.directory_path()):
                        return f"package folder is used by {process.title}"
                except (AttributeError, OSError):
                    pass

            if section == "Snapshots":
                support = getattr(process, "support", None)
                snapshots = (getattr(process, "snapshot", None), getattr(support, "kernel_snapshot", None))
                if any(same_path(getattr(snapshot, "path", None))
                       or os.path.basename(target) == getattr(snapshot, "filename", None)
                       for snapshot in snapshots if snapshot is not None):
                    return f"this snapshot is being used by {process.title}"

            if section == "Cache" and category == "Extracted":
                support = getattr(process, "support", None)
                snapshots = (getattr(process, "snapshot", None), getattr(support, "kernel_snapshot", None))
                for snapshot in (snapshot for snapshot in snapshots if snapshot is not None):
                    filename = getattr(snapshot, "filename", None)
                    if filename:
                        extracted_snapshot = os.path.join(
                            library_path, "Cache", "Extracted", "Snapshots", filename + ".d"
                        )
                        if os.path.realpath(extracted_snapshot) == target:
                            return f"extracted snapshot is being used by {process.title}"

                toolset = getattr(process, "toolset", None) or getattr(support, "kernel_toolset", None)
                if toolset is not None:
                    try:
                        squashfs = toolset.file_path()
                        candidates = [os.path.join(library_path, "Cache", "Extracted", "Toolsets",
                                                   os.path.basename(squashfs) + ".d")]
                        if toolset.machine:
                            from .rootless import machine_toolset_cache_image
                            candidates.append(machine_toolset_cache_image(squashfs, toolset.executor))
                        if any(os.path.realpath(candidate) == target for candidate in candidates):
                            return f"toolset {toolset.name} is used by {process.title}"
                    except Exception:
                        return "toolset cache usage could not be verified"

            if section == "Cache" and category == "Projects" and project_key:
                if relative.split("/", 1)[0] == project_key:
                    return f"project cache is being used by {process.title}"
            if section == "Cache" and category == "Packages" and project is not None:
                try:
                    package_dir = project.get_packages_directory()
                    package_id = str(getattr(package_dir, "id", "")).replace("-", "")
                    if package_id and relative.split("/", 1)[0] == package_id:
                        return f"binary package cache is being used by {process.title}"
                except (AttributeError, OSError):
                    pass

            if section == "Cache" and category == "Distfiles":
                # Portage operations share the source archive cache; an active build or kernel/toolset
                # emerge may read from or write to it.
                title = process.__class__.__name__
                if title in {"ProjectBuild", "ToolsetInstallation", "ToolsetUpdate", "QemuImagePreparation"}:
                    return f"source archives are being used by {process.title}"

            if section == "Cache" and category == "ToolsetStages" and process.__class__.__name__ == "ToolsetInstallation":
                url = getattr(process, "stage_url", None)
                if url and os.path.basename(url.path) == os.path.basename(target):
                    return "this stage archive is being downloaded or extracted"

            if section == "Cache" and category == "ToolsetBuilds":
                if any(same_path(getattr(stage, "toolset_squashfs_file", None))
                       for stage in getattr(process, "stages", [])):
                    return f"this toolset image is being created by {process.title}"

            if section == "Cache" and category == "TestVMImages":
                for stage in getattr(process, "stages", []):
                    if getattr(stage, "operation", None) == "cache":
                        try:
                            cache_path, _size = stage._cache_directory()
                            if os.path.realpath(cache_path) == target:
                                return f"this VM image is being prepared by {process.title}"
                        except (AttributeError, OSError, ValueError):
                            pass

            if section == "Cache" and category == "TestVMKernels":
                support = getattr(process, "support", None)
                if support and getattr(support, "kernel_package", None):
                    try:
                        from .qemu_run import _kernel_output_cache_directory
                        architecture = project.get_architecture() if project else None
                        toolset = support.kernel_toolset
                        if toolset and architecture:
                            cache = _kernel_output_cache_directory(
                                architecture.value, support.kernel_package, toolset
                            )
                            if os.path.realpath(cache) == target:
                                return f"this kernel cache is being used by {process.title}"
                    except (AttributeError, OSError, ValueError):
                        pass

        try:
            from .qemu_run import QemuRun
            for run in QemuRun.running_runs:
                if not run.is_running:
                    continue
                support = run.support
                run_project = getattr(run, "project_directory", None)
                run_project_id = str(getattr(run_project, "id", ""))
                if section == "Projects" and item.get("project_id") == run_project_id:
                    return "this project is used by a running virtual machine"
                if section == "Builds" and LibrarySection._same_or_inside(
                        getattr(getattr(run, "build", None), "path", None), target):
                    return "this build is used by a running virtual machine"
                if section == "Cache" and category == "TestVMImages" and LibrarySection._same_or_inside(
                        getattr(support, "base_image_path", None), target):
                    return "this VM base image is used by a running virtual machine"
                if section == "Cache" and category == "TestVMKernels" and LibrarySection._same_or_inside(
                        getattr(support, "base_kernel_path", None), target):
                    return "this kernel cache is used by a running virtual machine"
        except Exception as error:
            print(f"Could not verify active VM cache usage: {error}")
            if section == "Cache" and category in {"TestVMImages", "TestVMKernels"}:
                return "VM cache usage could not be verified"

        if section == "Cache" and category == "Extracted":
            try:
                from .repository import Repository
                for toolset in Repository.Toolset.value:
                    if not (toolset.spawned or toolset.in_use or toolset.is_reserved):
                        continue
                    squashfs = toolset.file_path()
                    candidates = [os.path.join(library_path, "Cache", "Extracted", "Toolsets",
                                               os.path.basename(squashfs) + ".d")]
                    if toolset.machine:
                        from .rootless import machine_toolset_cache_image
                        candidates.append(machine_toolset_cache_image(squashfs, toolset.executor))
                    if any(os.path.realpath(candidate) == target for candidate in candidates):
                        return f"toolset {toolset.name} is mounted or in use"
            except Exception as error:
                print(f"Could not verify extracted toolset usage: {error}")
                return "toolset cache usage could not be verified"
        return None

    def _refresh_safety(self):
        labels = {"Cache": self.cache_safety, "Packages": self.binpkgs_safety,
                  "Snapshots": self.snapshots_safety, "Projects": self.projects_safety,
                  "Builds": self.builds_safety}
        protected_sections = set()
        for button, section, item in self._delete_buttons:
            reason = self._item_use_reason(self.library_path, section, item)
            button.set_sensitive(not self._scanning and reason is None)
            button.set_tooltip_text(reason or f"Delete {section}/{item['name']}")
            if reason:
                protected_sections.add(section)
        for section, label in labels.items():
            message = "Some items are protected while active work is using them. Other items can still be removed."
            label.set_text(message)
            label.set_visible(section in protected_sections)

    def _confirm_delete(self, _button, section: str, item: dict):
        reason = self._item_use_reason(self.library_path, section, item)
        self._refresh_safety()
        if reason:
            return
        if section == "Projects":
            self._confirm_project_delete(item)
            return
        projects = item.get("projects", [])
        if section == "Packages":
            body = "Packages in it are deleted."
            if projects:
                body += (f" Projects {', '.join(projects)} won't have binary packages folder, "
                         "packages are kept in their builds.")
        elif section == "Snapshots":
            body = "This snapshot file and its extracted cache are deleted."
            if projects:
                body += f" Projects {', '.join(projects)} use this snapshot and may need another selected."
            body += " Existing build records remain unchanged."
        elif section == "Builds":
            body = (f"This removes the build output, artifact, logs, and failure details for "
                    f"{item['name']} ({item['details']}). This can't be undone.")
        else:
            body = (f"This permanently removes {section}/{item['category']}/{item['relative_path']} "
                    f"({format_size(item['size'])}).\n\n"
                    f"{item['description']} You may need to download or prepare this data again.")
        dialog = Adw.AlertDialog(
            heading=f"Delete {item['relative_path']}?",
            body=body,
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_response, section, item)
        dialog.present(self.get_root())

    def _confirm_project_delete(self, item: dict):
        from .repository import Repository

        project = next((project for project in Repository.ProjectDirectory.value
                        if str(project.id) == item["project_id"]), None)
        if project is None:
            self.overview_status.set_text(f"Could not find project {item['name']} in the project list; refresh and try again.")
            self.refresh()
            return

        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        builds_checkbox = Gtk.CheckButton(label="Builds, logs, caches and downloaded seeds")
        builds_checkbox.set_sensitive(item.get("build_count", 0) > 0)
        if item.get("build_count", 0) == 0:
            builds_checkbox.set_label("Builds, logs, caches and downloaded seeds (none found)")
        package_checkbox = Gtk.CheckButton(label="Binary packages")
        packages_directory = project.get_packages_directory()
        if packages_directory is None:
            package_checkbox.set_label("Binary packages (none assigned)")
            package_checkbox.set_sensitive(False)
        elif any(other is not project for other in packages_directory.projects):
            package_checkbox.set_label("Binary packages (shared with other projects)")
            package_checkbox.set_sensitive(False)
        extra.append(builds_checkbox)
        extra.append(package_checkbox)

        dialog = Adw.AlertDialog(
            heading=f"Delete {project.name}?",
            body=("The project files and settings will be deleted. This can't be undone. "
                  "Build data and binary packages are kept unless selected below."),
            extra_child=extra,
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_dialog, response):
            if response != "delete":
                return
            reason = self._item_use_reason(self.library_path, "Projects", item)
            self._refresh_safety()
            if reason:
                return
            try:
                if package_checkbox.get_active() and packages_directory is not None:
                    from .packages_directory import delete_packages_directory
                    delete_packages_directory(packages_directory)
                if builds_checkbox.get_active():
                    from .project_build import delete_project_builds
                    delete_project_builds(project)
                from .project_manager import ProjectManager
                ProjectManager.shared().remove_directory(project)
            except Exception as error:
                print(f"Failed to delete project {project.name}: {error}")
                self.overview_status.set_text(f"Could not delete project {project.name}: {error}")
            self.refresh()

        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    def _on_delete_response(self, _dialog, response: str, section: str, item: dict):
        if response != "delete":
            return
        reason = self._item_use_reason(self.library_path, section, item)
        self._refresh_safety()
        if reason:
            return
        try:
            remove_library_item(self.library_path, section, item["category"], item["relative_path"])
        except Exception as error:
            print(f"Failed to remove {section}/{item['name']}: {error}")
            self.overview_status.set_text(f"Could not remove {section}/{item['name']}: {error}")
        self.refresh()
