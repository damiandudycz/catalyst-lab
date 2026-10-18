from __future__ import annotations
import os, threading
from gi.repository import Gtk, Adw, GLib
from .build_machine import VMRunner
from .build_machine_manager import VMRunnerManager
from .repository import Repository, collapse_home
from .event_bus import SharedEvent
from .usage_graph import USAGE_INTERVAL_SECONDS, usage_row, format_gib

class VMRunnerDetailsView(Gtk.Box):
    """Details of a VM runner: basic information, status, actions (start, stop,
    delete), resources (changed while machine is stopped) and toolsets running in it."""

    def __init__(self, machine: VMRunner, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.machine = machine
        self.content_navigation_view = content_navigation_view
        self.working: str | None = None # Description of running action (Starting, Stopping, Applying settings).

        scrolled_window = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_start=24, margin_end=24, margin_top=6, margin_bottom=24)
        scrolled_window.set_child(content)
        self.append(scrolled_window)

        # Basic information
        information_group = Adw.PreferencesGroup(title="Basic information")
        self.name_row = Adw.EntryRow(title="Runner name", show_apply_button=True, text=machine.name)
        self.name_row.connect("changed", self.on_name_changed)
        self.name_row.connect("apply", self.on_name_apply)
        information_group.add(self.name_row)
        self.name_used_row = Adw.ActionRow(title="This runner name is already used.", visible=False)
        self.name_used_row.add_css_class("error")
        information_group.add(self.name_used_row)
        information_group.add(Adw.ActionRow(title="Lima instance", subtitle=machine.instance_name, subtitle_selectable=True))
        location = machine.machine_directory
        information_group.add(Adw.ActionRow(
            title="Location", subtitle_selectable=True,
            subtitle=collapse_home(location),
        ))
        content.append(information_group)

        # Status
        status_group = Adw.PreferencesGroup()
        status_row = Adw.ActionRow(title="Status")
        tags = Gtk.Box(valign=Gtk.Align.CENTER, spacing=8)
        self.spinner = Adw.Spinner(visible=False)
        tags.append(self.spinner)
        self.tag_state = self._tag(tags)
        self.tag_in_use = self._tag(tags, "In use", "accent")
        self.tag_automatic = self._tag(tags, "Stops when unused")
        self.tag_stopping_soon = self._tag(tags, "Stopping soon", "warning")
        self.tag_workspace = self._tag(tags, "Working space mounted")
        status_row.add_suffix(tags)
        status_group.add(status_row)
        # Operations using the runner, like reservations of toolsets.
        self.users_row = Adw.ExpanderRow(title="Used by")
        self.user_rows: list[Adw.ActionRow] = []
        status_group.add(self.users_row)
        content.append(status_group)

        # Usage of the running VM, read every few seconds while the view is shown.
        self.usage_group = Adw.PreferencesGroup(title="Usage", visible=False)
        self.cpu_usage_row = usage_row("Processors", "…")
        self.memory_usage_row = usage_row("Memory", "…")
        self.swap_usage_row = usage_row("Swap", "…")
        self.disk_usage_row = usage_row("System disk", "…")
        self.workspace_usage_row = usage_row("Working space", "…")
        for row in (self.cpu_usage_row, self.memory_usage_row, self.swap_usage_row, self.disk_usage_row, self.workspace_usage_row):
            self.usage_group.add(row)
        content.append(self.usage_group)
        self._previous_cpu: tuple[int, int] | None = None
        self._usage_timer: int | None = None
        self._reading_usage = False
        self.connect("map", lambda widget: self._start_usage_updates())
        self.connect("unmap", lambda widget: self._stop_usage_updates())

        # Actions
        actions_group = Adw.PreferencesGroup(title="Actions")
        actions = Gtk.Box(spacing=6, hexpand=True, homogeneous=True)
        self.start_button = self._action_button("Start", "media-playback-start-symbolic", self.on_start_clicked)
        self.stop_button = self._action_button("Stop", "media-playback-stop-symbolic", self.on_stop_clicked)
        self.delete_button = self._action_button("Delete", "trash-bin-trash-svgrepo-com-symbolic", self.on_delete_clicked)
        self.delete_button.add_css_class("destructive-action")
        for button in (self.start_button, self.stop_button, self.delete_button):
            actions.append(button)
        actions_group.add(actions)
        content.append(actions_group)

        # Resources
        resources_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.resources_group = Adw.PreferencesGroup(title="Resources")
        self.cpus_row = self._spin_row("Processors", "Cores available to this runner", 1, max(1, os.cpu_count() or 1))
        self.memory_row = self._spin_row("Memory", "GiB reserved from this computer while the runner runs", 2, 256)
        self.workspace_row = self._spin_row("Working space limit", "GiB, created while toolsets run and deleted when they finish", 20, 2048)
        self.swap_row = self._spin_row("Swap limit", "GiB, used when builds need more memory, takes space only while used. 0 disables swap", 0, 512)
        for row in (self.cpus_row, self.memory_row, self.swap_row, self.workspace_row):
            self.resources_group.add(row)
        resources_container.append(self.resources_group)
        self.resources_actions = Gtk.Box(homogeneous=True, hexpand=True, spacing=8)
        discard_button = self._action_button("Discard changes", "edit-undo-symbolic", lambda button: self._load_resources())
        self.apply_button = self._action_button("Apply changes", "check-circle-svgrepo-com-symbolic", self.on_apply_clicked)
        self.apply_button.add_css_class("suggested-action")
        self.resources_actions.append(discard_button)
        self.resources_actions.append(self.apply_button)
        resources_container.append(self.resources_actions)
        content.append(resources_container)

        machine.event_bus.subscribe(SharedEvent.STATE_UPDATED, self._on_machine_updated)
        self.connect("map", lambda widget: self._refresh_in_background())
        self._load_resources()
        self._update_display()

    # Usage

    def _start_usage_updates(self):
        if self._usage_timer is None:
            self._usage_timer = GLib.timeout_add_seconds(USAGE_INTERVAL_SECONDS, self._read_usage)
            self._read_usage()

    def _stop_usage_updates(self):
        if self._usage_timer is not None:
            GLib.source_remove(self._usage_timer)
            self._usage_timer = None
        self._previous_cpu = None

    def _clear_usage(self):
        for row in (self.cpu_usage_row, self.memory_usage_row, self.swap_usage_row, self.disk_usage_row, self.workspace_usage_row):
            row.graph.clear()
            row.set_subtitle("…")
        self._previous_cpu = None

    def _read_usage(self) -> bool:
        if self._reading_usage or self.machine._status != VMRunner.STATUS_RUNNING:
            return True
        self._reading_usage = True
        def work():
            usage = self.machine.read_usage()
            GLib.idle_add(self._show_usage, usage)
        threading.Thread(target=work, daemon=True).start()
        return True

    def _show_usage(self, usage: dict | None):
        self._reading_usage = False
        running = self.machine._status == VMRunner.STATUS_RUNNING
        self.usage_group.set_visible(running and usage is not None)
        if not running or usage is None:
            if not running:
                self._clear_usage() # Graphs start again when machine starts.
            return False
        # Processors: usage between this and previous reading.
        cpu = (usage["cpu_total"], usage["cpu_idle"])
        if self._previous_cpu and cpu[0] > self._previous_cpu[0]:
            busy = 1 - (cpu[1] - self._previous_cpu[1]) / (cpu[0] - self._previous_cpu[0])
            self._set_usage(self.cpu_usage_row, busy, f"{busy:.0%} of {usage['cpus']} cores · load {usage['load']:.1f}")
        self._previous_cpu = cpu
        if total := usage.get("MemTotal"):
            used = total - usage.get("MemAvailable", 0)
            self._set_usage(self.memory_usage_row, used / total, f"{format_gib(used)} of {format_gib(total)}")
        swap_total = usage.get("SwapTotal", 0)
        self.swap_usage_row.set_visible(swap_total > 0)
        if swap_total:
            used = swap_total - usage.get("SwapFree", 0)
            self._set_usage(self.swap_usage_row, used / swap_total, f"{format_gib(used)} of {format_gib(swap_total)}")
        disks = usage["disks"]
        if "/" in disks:
            used, size = disks["/"]
            self._set_usage(self.disk_usage_row, used / size if size else 0, f"{format_gib(used)} of {format_gib(size)}")
        from .lima import MACHINE_DATA_DIRECTORY
        workspace = disks.get(MACHINE_DATA_DIRECTORY)
        self.workspace_usage_row.set_visible(workspace is not None)
        if workspace:
            used, size = workspace
            self._set_usage(self.workspace_usage_row, used / size if size else 0, f"{format_gib(used)} of {format_gib(size)}")
        return False

    @staticmethod
    def _set_usage(row: Adw.ActionRow, fraction: float, text: str):
        row.graph.add(min(max(fraction, 0), 1))
        row.set_subtitle(text)

    # Widgets

    @staticmethod
    def _tag(box: Gtk.Box, label: str = "", style: str | None = None) -> Gtk.Label:
        tag = Gtk.Label(label=label)
        tag.add_css_class("tag-label")
        tag.add_css_class("caption-heading")
        if style:
            tag.add_css_class(style)
        box.append(tag)
        return tag

    @staticmethod
    def _action_button(label: str, icon_name: str, handler) -> Gtk.Button:
        button = Gtk.Button(child=Adw.ButtonContent(label=label, icon_name=icon_name))
        button.connect("clicked", handler)
        return button

    def _spin_row(self, title: str, subtitle: str, lower: int, upper: int) -> Adw.SpinRow:
        row = Adw.SpinRow(title=title, subtitle=subtitle,
                          adjustment=Gtk.Adjustment(lower=lower, upper=upper, step_increment=1, page_increment=4))
        row.connect("notify::value", lambda row, param: self._update_display())
        return row

    # Status

    def _refresh_in_background(self):
        threading.Thread(target=self.machine.refresh_status, daemon=True).start()

    def _on_machine_updated(self, machine):
        self._update_display()

    def _update_display(self):
        status = self.machine._status
        running = status == VMRunner.STATUS_RUNNING
        stopped = status == VMRunner.STATUS_STOPPED
        # Status
        self.spinner.set_visible(self.working is not None or status in (None, VMRunner.STATUS_STARTING, VMRunner.STATUS_STOPPING))
        self.tag_state.set_label(self.working or status or "Checking")
        for style in ("success", "error"):
            self.tag_state.remove_css_class(style)
        if not self.working and (running or status == VMRunner.STATUS_MISSING):
            self.tag_state.add_css_class("success" if running else "error")
        self.tag_in_use.set_visible(running and self.machine.is_used)
        self.tag_automatic.set_visible(running and self.machine.stops_when_unused and not self.machine.stop_scheduled)
        self.tag_stopping_soon.set_visible(running and self.machine.stop_scheduled)
        self.tag_workspace.set_visible(running and self.machine._workspace_users > 0)
        self._update_users()
        # Actions
        self.start_button.set_visible(not running)
        self.start_button.set_sensitive(self.working is None and stopped)
        self.stop_button.set_visible(running)
        self.stop_button.set_sensitive(self.working is None and not self.machine.is_used)
        self.delete_button.set_sensitive(self.working is None and not running)
        # Resources
        editable = self.working is None and stopped
        for row in (self.cpus_row, self.memory_row, self.swap_row, self.workspace_row):
            row.set_sensitive(editable)
        self.resources_group.set_description(None if editable else "Stop the machine to change its resources")
        self.resources_actions.set_visible(self._resources_changed())
        self.apply_button.set_sensitive(editable)

    def _update_users(self):
        users = self.machine.users
        for row in self.user_rows:
            self.users_row.remove(row)
        self.user_rows = [Adw.ActionRow(title=GLib.markup_escape_text(user)) for user in users]
        for row in self.user_rows:
            self.users_row.add_row(row)
        self.users_row.set_visible(bool(users))
        self.users_row.set_subtitle(f"{len(users)} operation{'s' if len(users) != 1 else ''}")

    def _run_action(self, description: str, action):
        """Runs machine action in background, showing it in status until it finishes."""
        self.working = description
        self._update_display()
        def work():
            error = None
            try:
                action()
            except Exception as e:
                error = str(e)
            def done():
                self.working = None
                self._update_display()
                if error:
                    dialog = Adw.AlertDialog(heading=f"{description} failed", body=error)
                    dialog.add_response("ok", "OK")
                    dialog.present(self.get_root())
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def on_start_clicked(self, button):
        self._run_action("Starting", self.machine.start)

    def on_stop_clicked(self, button):
        self._run_action("Stopping", self.machine.stop)

    # Name

    def on_name_changed(self, row):
        name = row.get_text().strip()
        self.name_used_row.set_visible(name != self.machine.name and not VMRunnerManager.shared().is_name_available(name, machine=self.machine))

    def on_name_apply(self, row):
        name = row.get_text().strip()
        if name == self.machine.name or not VMRunnerManager.shared().is_name_available(name, machine=self.machine):
            row.set_text(self.machine.name)
            self.name_used_row.set_visible(False)
            return
        self.machine.rename(name)

    # Resources

    def _edited_resources(self) -> tuple[int, int, int, int]:
        return (int(self.cpus_row.get_value()), int(self.memory_row.get_value()), int(self.workspace_row.get_value()),
                int(self.swap_row.get_value()))

    def _resources_changed(self) -> bool:
        return self._edited_resources() != (self.machine.cpus, self.machine.memory_gib, self.machine.workspace_gib, self.machine.swap_gib)

    def _load_resources(self):
        self.cpus_row.set_value(self.machine.cpus)
        self.memory_row.set_value(self.machine.memory_gib)
        self.workspace_row.set_value(self.machine.workspace_gib)
        self.swap_row.set_value(self.machine.swap_gib)
        self._update_display()

    def on_apply_clicked(self, button):
        cpus, memory_gib, workspace_gib, swap_gib = self._edited_resources()
        self._run_action("Applying changes", lambda: self.machine.change_resources(cpus, memory_gib, workspace_gib, swap_gib))

    # Delete

    def on_delete_clicked(self, button):
        body = f"VM runner \"{self.machine.name}\" will be deleted with its disk, including extracted toolsets and package caches stored in it. Files in shared folders are kept."
        dialog = Adw.AlertDialog(heading="Delete VM runner?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self.on_delete_response)
        dialog.present(self.get_root())

    def on_delete_response(self, dialog, response: str):
        if response != "delete":
            return
        VMRunnerManager.shared().remove_runner(self.machine)
        if self.content_navigation_view:
            self.content_navigation_view.pop()
