import os
from gi.repository import Gtk, Adw, Gio, GLib
from .app_section import AppSection
from .app_events import AppEvents, app_event_bus
from .repository import Repository, library_directory, store_library_directory, collapse_home
from .library import LibraryTarget, create_library, library_target
from .runner import HOST_RUNNER
from .git_identity_editor import GitIdentityEditor
from .git_identity import effective_git_identity

# ------------------------------------------------------------------------------
# Initial setup: optional library selection, then sections needed before first project (environments, releng
# directories, snapshots), shown one after another with Next / Finish button. Header back returns to previous step.

def initial_setup_steps(include_git_identity: bool | None = None) -> list[type | None]:
    if include_git_identity is None:
        include_git_identity = (
            not Repository.Settings.value.initial_setup_done or
            not effective_git_identity().configured
        )
    # Existing installations that lack an identity only need to complete this required step.
    if include_git_identity and Repository.Settings.value.initial_setup_done:
        return [GitIdentitySetupStep]
    steps: list[type | None] = [None, AppSection.EnvironmentsSection, AppSection.RelengSection,
                                AppSection.SnapshotsSection]
    if include_git_identity:
        steps.insert(1, GitIdentitySetupStep)
    return steps


class GitIdentitySetupStep(Gtk.Box):
    """Require an author name and email before completing first-run setup."""

    def __init__(self, **_kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.set_margin_start(24)
        self.set_margin_end(24)
        self.set_margin_top(24)
        self.set_margin_bottom(24)
        self.append(Gtk.Label(
            label="Catalyst Lab uses Git for operations across the app, including creating and updating projects and managing repositories. Choose your system Git identity or enter a name and email to use only for Git commands launched by Catalyst Lab.",
            wrap=True, xalign=0
        ))
        self.editor = GitIdentityEditor(setup_required=True)
        self.append(self.editor)

    def is_valid(self) -> bool:
        return self.editor.is_valid()

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

    def __init__(self, content_navigation_view: Adw.NavigationView, step: int = 0,
                 include_git_identity: bool | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.content_navigation_view = content_navigation_view
        self.step = step
        self.include_git_identity = (
            not Repository.Settings.value.initial_setup_done or not effective_git_identity().configured
            if include_git_identity is None else include_git_identity
        )
        steps = initial_setup_steps(self.include_git_identity)
        section = steps[step]
        if section is None:
            section_view = LibrarySelectionStep()
        elif section is GitIdentitySetupStep:
            section_view = section()
        else:
            section_view = section(content_navigation_view=content_navigation_view)
        self.library_selection_step = section_view if isinstance(section_view, LibrarySelectionStep) else None
        self.git_identity_step = section_view if isinstance(section_view, GitIdentitySetupStep) else None
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
        if self.git_identity_step is not None:
            button.set_sensitive(self.git_identity_step.is_valid())
            self.git_identity_step.editor.validity_changed = button.set_sensitive
        button.connect("clicked", self._on_finish_pressed if is_last else self._on_next_pressed)
        bar.set_end_widget(button)
        return bar

    @classmethod
    def push(cls, content_navigation_view: Adw.NavigationView, step: int = 0,
             include_git_identity: bool | None = None):
        # Later steps retain the first page's dynamic layout even after identity becomes complete.
        if include_git_identity is None:
            include_git_identity = (
                not Repository.Settings.value.initial_setup_done or
                not effective_git_identity().configured
            )
        steps = initial_setup_steps(include_git_identity)
        section = steps[step]
        title = ("Git identity" if section is GitIdentitySetupStep else
                 "Library" if section is None else section.section_details.title)
        content_navigation_view.push_view(cls(content_navigation_view, step, include_git_identity), title)

    def _on_next_pressed(self, _):
        if self.library_selection_step is not None:
            self.library_selection_step.confirm_location(
                lambda: InitialSetupPage.push(self.content_navigation_view, self.step + 1,
                                              self.include_git_identity)
            )
        else:
            InitialSetupPage.push(self.content_navigation_view, self.step + 1, self.include_git_identity)

    def _on_finish_pressed(self, _):
        settings = Repository.Settings.value
        if settings.default_runner_id is None and HOST_RUNNER.supported():
            settings.default_runner_id = HOST_RUNNER.id
        settings.initial_setup_done = True
        app_event_bus.emit(AppEvents.OPEN_APP_SECTION, AppSection.ProjectsSection)
