import os, threading, time
from gi.repository import Gtk, Adw, Gio, GLib, Pango
from .app_section import app_section, AppSection
from .repository import Repository, library_directory, collapse_home
from .settings import NavigationStyle, SettingsEvents
from .repository import RepositoryEvent
from .library import LibraryChange, library_target, change_problem, change_library
from .status_indicator import StatusIndicatorState
from .runner import HOST_RUNNER, runner_is_supported
from .git_identity_editor import GitIdentityEditor
from .toolset_selection import default_toolset

@app_section(title="Settings", icon="settings-svgrepo-com-symbolic", order=9_000)
@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/app_sections/settings/settings_section.ui')
class SettingsSection(Gtk.Box):
    """Settings of app: location of library, navigation style."""
    __gtype_name__ = "SettingsSection"

    location_row = Gtk.Template.Child()
    navigation_style_row = Gtk.Template.Child()
    pause_queue_row = Gtk.Template.Child()
    qemu_cpus_row = Gtk.Template.Child()
    qemu_memory_row = Gtk.Template.Child()
    default_toolset_list = Gtk.Template.Child()
    default_runner_list = Gtk.Template.Child()
    git_identity_container = Gtk.Template.Child()

    # Options of navigation style row, in order.
    NAVIGATION_STYLES = [NavigationStyle.MODAL, NavigationStyle.PUSH]

    def __init__(self, content_navigation_view: Adw.NavigationView, **kwargs):
        super().__init__(**kwargs)
        self.content_navigation_view = content_navigation_view
        self.git_identity_container.append(GitIdentityEditor())
        self.location_row.set_subtitle(collapse_home(library_directory()))
        self.navigation_style_row.set_selected(self.NAVIGATION_STYLES.index(Repository.Settings.value.navigation_style))
        self.navigation_style_row.connect("notify::selected", self._on_navigation_style_selected)
        self.pause_queue_row.set_active(Repository.Settings.value.pause_queue_on_failure)
        self.pause_queue_row.connect("notify::active", lambda row, _: setattr(Repository.Settings.value, "pause_queue_on_failure", row.get_active()))
        settings = Repository.Settings.value
        self.qemu_cpus_row.set_value(settings.qemu_cpus)
        self.qemu_cpus_row.connect("notify::value", lambda row, _: setattr(settings, "qemu_cpus", round(row.get_value())))
        self.qemu_memory_row.set_value(settings.qemu_memory_gib)
        self.qemu_memory_row.connect("notify::value", lambda row, _: setattr(settings, "qemu_memory_gib", round(row.get_value())))
        self._default_toolset_rows = []
        self._load_default_toolset_choices()
        self._default_runner_rows = []
        self._load_default_runner_choices()
        Repository.Toolset.event_bus.subscribe(RepositoryEvent.VALUE_CHANGED, self._toolsets_changed, self)
        Repository.Runner.event_bus.subscribe(RepositoryEvent.VALUE_CHANGED, self._runners_changed, self)
        settings.event_bus.subscribe(SettingsEvents.DEFAULT_TOOLSET_CHANGED, self._default_toolset_changed, self)
        settings.event_bus.subscribe(SettingsEvents.DEFAULT_RUNNER_CHANGED, self._default_runner_changed, self)

    def _load_default_toolset_choices(self, *_):
        for row in self._default_toolset_rows:
            self.default_toolset_list.remove(row)
        self._default_toolset_rows.clear()
        settings = Repository.Settings.value
        group = Gtk.CheckButton()
        toolsets = Repository.Toolset.value
        choices = [(str(toolset.uuid), toolset.name, toolset.short_details)
                   for toolset in sorted(toolsets, key=lambda item: item.name.lower())]
        selected_id = settings.default_toolset_id
        if selected_id is None:
            selected = default_toolset(applications=(), toolsets=toolsets)
            selected_id = str(selected.uuid) if selected else None
        for identifier, title, subtitle in choices:
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            button = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            button.set_group(group)
            button.set_active(identifier == selected_id)
            button.connect("toggled", self._on_default_toolset_toggled, identifier)
            row.add_prefix(button)
            row.set_activatable_widget(button)
            self.default_toolset_list.append(row)
            self._default_toolset_rows.append(row)

    def _toolsets_changed(self, *_):
        self._load_default_toolset_choices()

    def _default_toolset_changed(self, *_):
        self._load_default_toolset_choices()

    def _on_default_toolset_toggled(self, button, identifier):
        if button.get_active():
            Repository.Settings.value.default_toolset_id = identifier

    def _load_default_runner_choices(self, *_):
        for row in self._default_runner_rows:
            self.default_runner_list.remove(row)
        self._default_runner_rows.clear()
        settings = Repository.Settings.value
        choices = [("host", "Host", "Runs primary tools directly on this computer", HOST_RUNNER)]
        choices.extend((str(runner.id), runner.name, runner.short_details, runner)
                       for runner in sorted(Repository.Runner.value, key=lambda item: item.name.lower()))
        choice_ids = {identifier for identifier, *_ in choices}
        selected_id = settings.default_runner_id
        if selected_id not in choice_ids:
            selected = default_runner()
            selected_id = str(selected.id) if selected else None
        group = Gtk.CheckButton()
        for identifier, title, subtitle, runner in choices:
            row = Adw.ActionRow(title=title, subtitle=subtitle or "")
            button = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            button.set_group(group)
            button.set_active(identifier == selected_id)
            button.connect("toggled", self._on_default_runner_toggled, identifier)
            row.add_prefix(button)
            row.set_activatable_widget(button)
            if runner is not None and not runner_is_supported(runner):
                row.set_sensitive(False)
            self.default_runner_list.append(row)
            self._default_runner_rows.append(row)

    def _runners_changed(self, *_):
        self._load_default_runner_choices()

    def _default_runner_changed(self, *_):
        self._load_default_runner_choices()

    def _on_default_runner_toggled(self, button, identifier):
        if button.get_active():
            Repository.Settings.value.default_runner_id = identifier

    def _on_navigation_style_selected(self, row, _param):
        Repository.Settings.value.navigation_style = self.NAVIGATION_STYLES[row.get_selected()]

    # Library location:

    @Gtk.Template.Callback()
    def on_open_clicked(self, button):
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(library_directory()).get_uri(), None)

    @Gtk.Template.Callback()
    def on_change_clicked(self, button):
        if self._show_if_busy():
            return
        dialog = Gtk.FileDialog(title="Choose Library Folder", modal=True)
        dialog.set_initial_folder(Gio.File.new_for_path(os.path.dirname(library_directory())))
        dialog.select_folder(self.get_root(), None, self._on_folder_selected)

    def _on_folder_selected(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return # Canceled.
        if folder is None or folder.get_path() is None:
            return
        try:
            target = library_target(folder.get_path())
        except ValueError as e:
            self._show_message("Can't Use This Folder", str(e))
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
        dialog.connect("response", lambda dialog, response: self._on_change_confirmed(target, response))
        dialog.present(self)

    def _on_change_confirmed(self, target, response: str):
        if response == "cancel":
            return
        change = LibraryChange(response)
        if self._show_if_busy(): # Something could start while dialog was shown.
            return
        if problem := change_problem(target, change):
            self._show_message("Can't Change Library", problem)
            return
        LibraryChangeDialog(target, change).present(self)

    def _show_if_busy(self) -> bool:
        """Library can't change while something uses it, user is told what."""
        items = items_using_library()
        if items:
            self._show_message("Library Is in Use", "Stop these and try again:\n\n" + "\n".join(f"• {item}" for item in items))
        return bool(items)

    def _show_message(self, heading: str, body: str):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.present(self)

def items_using_library() -> list[str]:
    """Running operations and loaded items (mounted environments, running machines, builds), from statuses of sections
    (like in side menu)."""
    from .multistage_process import MultiStageProcess, MultiStageProcessState
    items = []
    for section in AppSection.all_sections:
        if (section_status := getattr(section, "section_status", None)) is None:
            continue
        for entry in section_status():
            values = entry.values
            if values is None:
                continue
            if values.blinking or values.state == StatusIndicatorState.LOADED or any(
                    detail.state == StatusIndicatorState.LOADED for detail in values.details):
                items.append(entry.label or section.section_details.title)
    items += [process.title for process in MultiStageProcess.started_processes
              if process.status == MultiStageProcessState.IN_PROGRESS]
    return list(dict.fromkeys(items))

class LibraryChangeDialog(Adw.Dialog):
    """Progress of library change, app restarts when it's done."""

    def __init__(self, target, change: LibraryChange):
        super().__init__(title="Changing Library", can_close=False, content_width=420)
        self.target = target
        self.change = change
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=24, margin_end=24,
                      margin_top=24, margin_bottom=24)
        title = {LibraryChange.MOVE: "Moving library", LibraryChange.COPY: "Copying library"}.get(change, "Changing library")
        heading = Gtk.Label(label=f"{title} to {collapse_home(target.path)}", wrap=True, xalign=0)
        heading.add_css_class("heading")
        self.progress_bar = Gtk.ProgressBar()
        self.status_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.status_label.add_css_class("dimmed")
        self.status_label.add_css_class("caption")
        box.append(heading)
        box.append(self.progress_bar)
        box.append(self.status_label)
        self.set_child(box)
        self._progress = (0.0, "")
        self._last_update = 0.0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            warnings = change_library(self.target, self.change, self._on_progress)
            GLib.idle_add(self._finished, None, warnings)
        except Exception as e:
            GLib.idle_add(self._finished, e, [])

    def _on_progress(self, fraction: float, text: str):
        self._progress = (fraction, text)
        now = time.monotonic()
        if now - self._last_update > 0.1: # UI is updated at most 10 times a second.
            self._last_update = now
            GLib.idle_add(self._show_progress)

    def _show_progress(self):
        fraction, text = self._progress
        self.progress_bar.set_fraction(fraction)
        self.status_label.set_label(text)
        return False

    def _finished(self, error: Exception | None, warnings: list[str]):
        self.set_can_close(True)
        self.close()
        if error is not None:
            print(f"Failed to change library: {error}")
            dialog = Adw.AlertDialog(heading="Library Was Not Changed", body=f"{error}\n\nCurrent library is still used.")
            dialog.add_response("ok", "OK")
            dialog.present(Gtk.Application.get_default().get_active_window())
            return False
        if warnings:
            print("Files of old library that couldn't be removed:\n" + "\n".join(warnings))
            dialog = Adw.AlertDialog(heading="Some Files Were Not Removed", body=(
                f"Library was moved, but {len(warnings)} files of the old library couldn't be removed:\n\n"
                + "\n".join(warnings[:5]) + ("\n…" if len(warnings) > 5 else "")))
            dialog.add_response("restart", "Restart")
            dialog.connect("response", lambda *args: Gtk.Application.get_default().restart())
            dialog.present(Gtk.Application.get_default().get_active_window())
            return False
        Gtk.Application.get_default().restart()
        return False
