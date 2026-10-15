import os
from gi.repository import Gtk, Adw, Gio, GLib
from .app_section import AppSection
from .app_events import AppEvents, app_event_bus
from .repository import Repository, library_directory, store_library_directory, collapse_home
from .library import is_library, LIBRARY_FOLDER_NAME

# ------------------------------------------------------------------------------
# Initial setup: optional library selection, then sections needed before first project (environments, releng
# directories, snapshots), shown one after another with Next / Finish button. Header back returns to previous step.

def initial_setup_steps() -> list[AppSection | None]:
    return [None, AppSection.EnvironmentsSection, AppSection.RelengSection, AppSection.SnapshotsSection]

class LibrarySelectionStep(Gtk.Box):
    """Optional first-run switch to a library that already exists."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.set_margin_start(24)
        self.set_margin_end(24)
        self.set_margin_top(24)
        self.set_margin_bottom(24)
        description = Gtk.Label(
            label=("If you already have a Catalyst Lab library, select it here. Otherwise continue and set up a new "
                   "library at the current location."),
            wrap=True, xalign=0
        )
        self.append(description)
        group = Adw.PreferencesGroup(title="Library location")
        row = Adw.ActionRow(title="Current location", subtitle=collapse_home(library_directory()))
        row.set_subtitle_selectable(True)
        group.add(row)
        self.append(group)
        button = Gtk.Button(label="Choose Existing Library…", halign=Gtk.Align.START)
        button.connect("clicked", self._choose_library)
        self.append(button)

    def _choose_library(self, _button):
        dialog = Gtk.FileDialog(title="Choose Existing Library", modal=True)
        dialog.set_initial_folder(Gio.File.new_for_path(os.path.dirname(library_directory())))
        dialog.select_folder(self.get_root(), None, self._on_folder_selected)

    def _on_folder_selected(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is None or folder.get_path() is None:
            return
        chosen = os.path.abspath(os.path.expanduser(folder.get_path()))
        candidates = (chosen, os.path.join(chosen, LIBRARY_FOLDER_NAME))
        target = next((path for path in candidates if is_library(path)), None)
        if target is None:
            self._show_message(
                "No Library Found",
                "Choose a folder containing a Catalyst Lab library, or a folder whose CatalystLab subfolder contains one."
            )
            return
        if os.path.realpath(target) == os.path.realpath(library_directory()):
            self._show_message("Library Already Selected", "This library is already in use.")
            return
        confirmation = Adw.AlertDialog(
            heading="Use Existing Library?",
            body=f"Catalyst Lab will restart and use {collapse_home(target)}. The library will not be moved or changed."
        )
        confirmation.add_response("cancel", "Cancel")
        confirmation.add_response("switch", "Use Library and Restart")
        confirmation.set_response_appearance("switch", Adw.ResponseAppearance.SUGGESTED)
        confirmation.set_default_response("cancel")
        confirmation.set_close_response("cancel")
        confirmation.connect("response", self._on_switch_response, target)
        confirmation.present(self)

    def _on_switch_response(self, _dialog, response: str, target: str):
        if response != "switch":
            return
        try:
            store_library_directory(target)
        except OSError as error:
            self._show_message("Can't Select Library", str(error))
            return
        Gtk.Application.get_default().restart()

    def _show_message(self, heading: str, body: str):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.present(self)

class InitialSetupPage(Gtk.Box):
    """Section of initial setup step with bottom bar."""

    def __init__(self, content_navigation_view: Adw.NavigationView, step: int = 0):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.content_navigation_view = content_navigation_view
        self.step = step
        steps = initial_setup_steps()
        section = steps[step]
        section_view = (LibrarySelectionStep() if section is None else
                        section(content_navigation_view=content_navigation_view))
        section_view.set_vexpand(True)
        self.append(section_view)
        self.append(Gtk.Separator())
        self.append(self._bottom_bar(is_last=step == len(steps) - 1, steps_count=len(steps)))

    def _bottom_bar(self, is_last: bool, steps_count: int) -> Gtk.Widget:
        bar = Gtk.CenterBox()
        bar.set_margin_start(24)
        bar.set_margin_end(24)
        bar.set_margin_top(12)
        bar.set_margin_bottom(12)
        label = Gtk.Label(label=f"Step {self.step + 1} of {steps_count}")
        label.add_css_class("dimmed")
        bar.set_center_widget(label)
        button = Gtk.Button(label="Finish" if is_last else "Next")
        button.add_css_class("suggested-action")
        button.connect("clicked", self._on_finish_pressed if is_last else self._on_next_pressed)
        bar.set_end_widget(button)
        return bar

    @classmethod
    def push(cls, content_navigation_view: Adw.NavigationView, step: int = 0):
        section = initial_setup_steps()[step]
        title = "Library" if section is None else section.section_details.title
        content_navigation_view.push_view(cls(content_navigation_view, step), title)

    def _on_next_pressed(self, _):
        InitialSetupPage.push(self.content_navigation_view, self.step + 1)

    def _on_finish_pressed(self, _):
        Repository.Settings.value.initial_setup_done = True
        app_event_bus.emit(AppEvents.OPEN_APP_SECTION, AppSection.ProjectsSection)
