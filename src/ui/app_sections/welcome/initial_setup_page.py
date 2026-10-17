import os
from gi.repository import Gtk, Adw, Gio, GLib
from .app_section import AppSection
from .app_events import AppEvents, app_event_bus
from .repository import Repository, library_directory, store_library_directory, collapse_home
from .library import LibraryTarget, create_library, library_target

# ------------------------------------------------------------------------------
# Initial setup: optional library selection, then sections needed before first project (environments, releng
# directories, snapshots), shown one after another with Next / Finish button. Header back returns to previous step.

def initial_setup_steps() -> list[AppSection | None]:
    return [None, AppSection.EnvironmentsSection, AppSection.RelengSection, AppSection.SnapshotsSection]

class LibrarySelectionStep(Gtk.Box):
    """Choose an existing Library or a location where a new one will be created after confirmation."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.set_margin_start(24)
        self.set_margin_end(24)
        self.set_margin_top(24)
        self.set_margin_bottom(24)
        description = Gtk.Label(
            label="Choose where Catalyst Lab should keep its Library. Existing Libraries are detected automatically.",
            wrap=True, xalign=0
        )
        self.append(description)
        self.target: LibraryTarget | None = None
        self.selection_error: str | None = None
        self.location = library_directory()
        group = Adw.PreferencesGroup(title="Library location")
        self.location_row = Adw.ActionRow(title="Library location")
        self.location_row.set_subtitle_selectable(True)
        choose_button = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose_button.connect("clicked", self._choose_location)
        self.location_row.add_suffix(choose_button)
        group.add(self.location_row)
        self.append(group)
        self._update_location(self.location)

    def _choose_location(self, _button):
        dialog = Gtk.FileDialog(title="Choose Library Location", modal=True)
        parent = os.path.dirname(self.location)
        while parent and not os.path.isdir(parent):
            next_parent = os.path.dirname(parent)
            if next_parent == parent:
                break
            parent = next_parent
        if parent and os.path.isdir(parent):
            dialog.set_initial_folder(Gio.File.new_for_path(parent))
        dialog.select_folder(self.get_root(), None, self._on_folder_selected)

    def _on_folder_selected(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is None or folder.get_path() is None:
            return
        self._update_location(folder.get_path())

    def _update_location(self, chosen: str):
        self.location = os.path.abspath(os.path.expanduser(chosen))
        try:
            self.target = library_target(self.location, allow_current=True)
            state = "Existing Library found" if self.target.existing else "A new Library will be created after confirmation"
            self.selection_error = None
            subtitle = f"{collapse_home(self.target.path)}\n{state}"
        except ValueError as error:
            self.target = None
            self.selection_error = str(error)
            subtitle = f"{collapse_home(self.location)}\n{self.selection_error}"
        self.location_row.set_subtitle(subtitle)

    def confirm_location(self, on_confirmed):
        self._update_location(self.location)
        if self.target is None:
            self._show_message("Can't Use This Location", self.selection_error or "Choose another location.")
            return
        target = self.target
        location = collapse_home(target.path)
        if target.existing:
            heading = "Use Existing Library?"
            body = f"Catalyst Lab found a Library at {location}. It will use this Library."
            response_id, response_label = "use", "Use Library"
        else:
            heading = "Create Library?"
            body = f"Create a new Library at {location}? No Library folders will be created until you confirm."
            response_id, response_label = "create", "Create Library"
        confirmation = Adw.AlertDialog(heading=heading, body=body)
        confirmation.add_response("cancel", "Cancel")
        confirmation.add_response(response_id, response_label)
        confirmation.set_response_appearance(response_id, Adw.ResponseAppearance.SUGGESTED)
        confirmation.set_default_response("cancel")
        confirmation.set_close_response("cancel")
        confirmation.connect("response", self._on_location_confirmed, target, on_confirmed)
        confirmation.present(self)

    def _on_location_confirmed(self, _dialog, response: str, target: LibraryTarget, on_confirmed):
        if response not in ("use", "create"):
            return
        try:
            create_library(target.path)
            store_library_directory(target.path)
            app = Gtk.Application.get_default()
            app.initialize_library(reload_repositories=True)
        except (OSError, ValueError) as error:
            self._show_message("Can't Set Up Library", str(error))
            return
        on_confirmed()

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
        self.library_selection_step = section_view if isinstance(section_view, LibrarySelectionStep) else None
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
        if self.library_selection_step is not None:
            self.library_selection_step.confirm_location(
                lambda: InitialSetupPage.push(self.content_navigation_view, self.step + 1)
            )
        else:
            InitialSetupPage.push(self.content_navigation_view, self.step + 1)

    def _on_finish_pressed(self, _):
        Repository.Settings.value.initial_setup_done = True
        app_event_bus.emit(AppEvents.OPEN_APP_SECTION, AppSection.ProjectsSection)
