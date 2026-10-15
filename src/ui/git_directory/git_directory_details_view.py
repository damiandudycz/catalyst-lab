from __future__ import annotations
from gi.repository import Gtk, GLib, Gio, Adw, GObject
from .git_directory import GitDirectoryEvent, GitDirectoryStatus
from .git_manager import GitManager
from .git_update import GitUpdate
import threading, os
from .multistage_process import MultiStageProcess, MultiStageProcessEvent, MultiStageProcessState
from .multistage_process_execution_view import MultistageProcessExecutionView
from .event_bus import SharedEvent

# Import additional classes so that they can be parsed for self.manager_class and self.update_class_name:
from .releng_manager import RelengManager
from .releng_update import RelengUpdate
from .overlay_manager import OverlayManager
from .overlay_update import OverlayUpdate
from .project_manager import ProjectManager
from .project_update import ProjectUpdate

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/git_directory/git_directory_details_view.ui')
class GitDirectoryDetailsView(Gtk.Box):
    __gtype_name__ = "GitDirectoryDetailsView"

    directory_name_row = Gtk.Template.Child()
    name_used_row = Gtk.Template.Child()
    status_directory_url_row = Gtk.Template.Child()
    status_directory_branch_name_row = Gtk.Template.Child()
    status_directory_date_updated_row = Gtk.Template.Child()
    status_directory_path_row = Gtk.Template.Child()
    status_logs_row = Gtk.Template.Child()
    tag_unknown = Gtk.Template.Child()
    tag_unchanged = Gtk.Template.Child()
    tag_changed = Gtk.Template.Child()
    tag_updating = Gtk.Template.Child()
    tag_update_succeded = Gtk.Template.Child()
    tag_update_failed = Gtk.Template.Child()
    tag_update_available = Gtk.Template.Child()
    action_button_save_changes = Gtk.Template.Child()
    action_button_update = Gtk.Template.Child()
    action_button_delete = Gtk.Template.Child()
    status_update_row = Gtk.Template.Child()
    status_update_progress_label = Gtk.Template.Child()

    # Use these properties if using GitDirectoryDetailsView in .ui file.
    # If created in code, pass manager_class and update_class in initializer.
    manager_class_name = GObject.Property(type=str, default=None)
    update_class_name = GObject.Property(type=str, default=None)

    def __init__(self, git_directory: GitDirectory | None = None, manager_class: Type[GitManager] | None = None, update_class: Type[GitUpdate] | None = None):
        super().__init__()
        self.update_in_progress: GitUpdate | None = None
        self.git_directory = git_directory
        self.manager_class = manager_class
        self.update_class = update_class
        # Actions of Save changes menu are inserted before view is added to window. Inserted later (when realized),
        # menu items don't find them and stay disabled.
        self._changes_action_group = Gio.SimpleActionGroup()
        self._add_changes_action("save_changes", self.save_changes)
        self._add_changes_action("discard_changes", self.discard_changes)
        self.insert_action_group("changes", self._changes_action_group)
        self.connect("realize", self.on_realize)

    def setup(self, git_directory: GitDirectory, content_navigation_view: Adw.NavigationView | None = None):
        """Call this after init, before view appears."""
        self.git_directory = git_directory
        self.content_navigation_view = content_navigation_view

    def on_realize(self, widget):
        self.get_root().set_focus(None)
        if self.manager_class_name and self.manager_class is None:
            self.manager_class = globals().get(self.manager_class_name)
        if self.update_class_name and self.update_class is None:
            self.update_class = globals().get(self.update_class_name)
        self.setup_git_directory_details()
        self.setup_git_directory_logs()
        self.load_update_state()
        self.setup_status()
        self.git_directory.event_bus.subscribe(SharedEvent.STATE_UPDATED, self.setup_git_directory_details)
        self.git_directory.event_bus.subscribe(GitDirectoryEvent.LOGS_CHANGED, self.setup_git_directory_logs)
        MultiStageProcess.event_bus.subscribe(MultiStageProcessEvent.STARTED_PROCESSES_CHANGED, self.git_directories_updates_updated)

    def setup_git_directory_details(self, object = None):
        if object is not None and object != self.git_directory:
            return
        """Displays main details of the git directory."""
        self.directory_name_row.set_text(self.git_directory.name)
        last_commit_date = self.git_directory.last_commit_date
        self.status_directory_url_row.set_subtitle(self.git_directory.remote_url or "(local)")
        self.status_directory_branch_name_row.set_subtitle(self.git_directory.branch_name or "(none)")
        self.status_directory_date_updated_row.set_subtitle(last_commit_date.strftime("%Y-%d-%m %H:%M") if last_commit_date else "unknown")
        self.status_directory_path_row.set_subtitle(self.git_directory.directory_path())
        self.setup_status()
        if object is None:
            self.git_directory.update_status()
            self.git_directory.update_logs()

    def setup_git_directory_logs(self, event_data = None):
        if hasattr(self, "_log_rows"):
            for row in self._log_rows:
                self.status_logs_row.remove(row)
        self.status_logs_row.set_expanded(False)
        self._log_rows = []
        max_logs = 10
        if self.git_directory.logs:
            i = 1
            for log in self.git_directory.logs:
                message = log.get("message")
                author = log.get("author")
                date = log.get("date")
                row = Adw.ActionRow(title=message, subtitle=f"{date.strftime("%Y-%d-%m %H:%M")}, {author}")
                self.status_logs_row.add_row(row)
                self._log_rows.append(row)
                i += 1
                if i > max_logs:
                    break

    def setup_status(self, _ = None):
        """Updates controls visibility and sensitivity for current status."""
        self.tag_unknown.set_visible(self.git_directory.status in (GitDirectoryStatus.UNKNOWN, GitDirectoryStatus.ERROR))
        self.tag_unknown.set_label("Error" if self.git_directory.status == GitDirectoryStatus.ERROR else "Unknown")
        self.tag_unchanged.set_visible(self.git_directory.status == GitDirectoryStatus.UNCHANGED)
        self.tag_update_available.set_visible(self.git_directory.has_remote_changes)
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
        self.status_update_row.set_visible(self.update_in_progress)
        self.status_update_row.set_subtitle(
            "" if not self.update_in_progress
            else "Update in progress" if self.update_in_progress.status == MultiStageProcessState.IN_PROGRESS
            else "Update completed" if self.update_in_progress.status == MultiStageProcessState.COMPLETED
            else "Update failed" if self.update_in_progress.status == MultiStageProcessState.FAILED
            else ""
        )
        if self.git_directory.has_remote_changes:
            self.action_button_update.get_style_context().add_class("suggested-action")
        else:
            self.action_button_update.get_style_context().remove_class("suggested-action")
        self.status_update_progress_label.set_visible(
            self.update_in_progress
            and self.update_in_progress.status == MultiStageProcessState.IN_PROGRESS
        )
        self.tag_changed.set_visible(self.git_directory.status in (GitDirectoryStatus.CHANGED, GitDirectoryStatus.CONFLICTED))
        self.tag_changed.set_label("Conflicts" if self.git_directory.status == GitDirectoryStatus.CONFLICTED else "Changed")
        self.action_button_save_changes.set_sensitive(
            self.git_directory.status == GitDirectoryStatus.CHANGED
            and (
                not self.update_in_progress
                or self.update_in_progress.status == MultiStageProcessState.COMPLETED
                or self.update_in_progress.status == MultiStageProcessState.FAILED
            )
        )
        self.action_button_update.set_sensitive(
            self.git_directory.status == GitDirectoryStatus.UNCHANGED
            and (
                not self.update_in_progress
                or self.update_in_progress.status == MultiStageProcessState.COMPLETED
                or self.update_in_progress.status == MultiStageProcessState.FAILED
            )
            and self.git_directory.remote_url is not None
        )

    def load_update_state(self, started_processes: list[GitUpdate] | None = None):
        if started_processes is None:
            started_processes = MultiStageProcess.get_started_processes_by_class(GitUpdate)
        if self.update_in_progress is not None :
            self.update_in_progress.event_bus.unsubscribe(MultiStageProcessEvent.STATE_CHANGED, self)
        self.update_in_progress = next((process for process in started_processes if process.directory == self.git_directory), None)
        if self.update_in_progress:
            self.update_in_progress.event_bus.subscribe(
                MultiStageProcessEvent.STATE_CHANGED,
                self.git_directories_update_process_state_changed,
                self
            )
            self.status_update_progress_label.set_label(f"{int(self.update_in_progress.progress * 100)}%")
            self.update_in_progress.event_bus.subscribe(
                MultiStageProcessEvent.PROGRESS_CHANGED,
                self.git_directories_update_process_progress_changed,
                self
            )

    def git_directories_updates_updated(self, process_class: type[MultiStageProcess], started_processes: list[MultiStageProcess]):
        if issubclass(process_class, GitUpdate):
            self.load_update_state(started_processes=started_processes)
            self.setup_status()

    def git_directories_update_process_state_changed(self, state: MultiStageProcessState):
        self.setup_status()

    def git_directories_update_process_progress_changed(self, progress: float):
        self.status_update_progress_label.set_label(f"{int(progress * 100)}%")

    @Gtk.Template.Callback()
    def on_directory_name_activate(self, sender):
        new_name = self.directory_name_row.get_text()
        if new_name == self.git_directory.name:
            self.get_root().set_focus(None)
            return
        is_name_available = self.manager_class.shared().is_name_available(name=new_name)
        try:
            if not is_name_available:
                raise RuntimeError(f"Git directory name {new_name} is not available")
            self.manager_class.shared().rename_directory(directory=self.git_directory, name=new_name)
            self.get_root().set_focus(None)
            self.setup_git_directory_details()
        except Exception as e:
            print(f"Error renaming git directory: {e}")
            self.directory_name_row.add_css_class("error")
            self.directory_name_row.grab_focus()

    @Gtk.Template.Callback()
    def on_directory_name_changed(self, sender):
        is_name_available = self.manager_class.shared().is_name_available(name=self.directory_name_row.get_text()) or self.directory_name_row.get_text() == self.git_directory.name
        self.name_used_row.set_visible(not is_name_available)
        self.directory_name_row.remove_css_class("error")

    @Gtk.Template.Callback()
    def status_update_row_clicked(self, sender):
        self.show_update(update=self.update_in_progress)

    @Gtk.Template.Callback()
    def action_button_save_changes_clicked(self, sender):
        self.ask_commit_message()

    def save_changes(self, action, param):
        self.ask_commit_message()

    def ask_commit_message(self):
        """Dialog with commit message of saved changes."""
        entry = Gtk.Entry(placeholder_text=self.git_directory.DEFAULT_COMMIT_MESSAGE, activates_default=True)
        dialog = Adw.AlertDialog(heading="Save changes", body="Describe the changes, they are committed to Git repository of this directory.",
                                 extra_child=entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")
        def on_response(dialog, response):
            if response == "save":
                self.git_directory.commit_changes(message=entry.get_text())
        dialog.connect("response", on_response)
        dialog.present(self.get_root())
        entry.grab_focus()

    def discard_changes(self, action, param):
        self.git_directory.discard_changes()

    @Gtk.Template.Callback()
    def action_button_update_clicked(self, sender):
        self.start_update()

    @Gtk.Template.Callback()
    def action_button_delete_clicked(self, sender):
        is_project = self.manager_class is ProjectManager
        extra_child = None
        builds_checkbox = binpkgs_checkbox = None
        build_running = False
        if is_project:
            extra_child = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            builds_checkbox = Gtk.CheckButton(label="Builds, logs, caches and downloaded seeds")
            binpkgs_checkbox = Gtk.CheckButton(label="Binary packages")
            extra_child.append(builds_checkbox)
            extra_child.append(binpkgs_checkbox)
            from .project_build_process import running_project_build
            build_running = running_project_build(self.git_directory) is not None
            if build_running:
                builds_checkbox.set_label("Builds (a build is running; stop it before deleting)")
                builds_checkbox.set_sensitive(False)
            get_packages_directory = getattr(self.git_directory, "get_packages_directory", None)
            packages_directory = get_packages_directory() if get_packages_directory else None
            other_projects_use_packages = packages_directory is not None and any(
                project is not self.git_directory for project in packages_directory.projects)
            if packages_directory is None:
                binpkgs_checkbox.set_label("Binary packages (none assigned)")
                binpkgs_checkbox.set_sensitive(False)
            elif other_projects_use_packages:
                binpkgs_checkbox.set_label("Binary packages (shared with other projects)")
                binpkgs_checkbox.set_sensitive(False)
        dialog = Adw.AlertDialog(
            heading=f"Delete {self.git_directory.name}?",
            body=("The project files and settings will be deleted. This can't be undone."
                  if is_project else "The directory and its files will be deleted. This can't be undone."),
            extra_child=extra_child,
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(dialog, response):
            if response != "delete":
                return
            dialog_parent = self.get_root()
            delete_builds = builds_checkbox is not None and builds_checkbox.get_active()
            delete_binpkgs = binpkgs_checkbox is not None and binpkgs_checkbox.get_active()
            project = self.git_directory
            packages_directory = project.get_packages_directory() if delete_binpkgs else None
            if packages_directory is not None:
                from .packages_directory import delete_packages_directory
                try:
                    # Delete the selected package folder while the project still
                    # exists in the repository. This keeps its association intact
                    # until the filesystem removal succeeds.
                    delete_packages_directory(packages_directory)
                except Exception as error:
                    def show_packages_error(error=error):
                        error_dialog = Adw.AlertDialog(
                            heading="Binary packages couldn't be deleted",
                            body=f"The project was kept because its binary package folder could not be removed: {error}",
                        )
                        error_dialog.add_response("close", "Close")
                        error_dialog.present(dialog_parent)
                        return False
                    GLib.idle_add(show_packages_error)
                    return
            if delete_builds:
                from .project_build import delete_project_builds
                delete_project_builds(project)
            self.manager_class.shared().remove_directory(directory=project)
            close_project_view = getattr(self, "on_project_deleted", None) if is_project else None
            if close_project_view is not None:
                # Let the delete dialog finish responding before changing the navigation stack.
                GLib.idle_add(close_project_view)
            if close_project_view is None and hasattr(self, "_window"):
                self._window.close()
            elif close_project_view is None and hasattr(self, "content_navigation_view"):
                self.content_navigation_view.pop()

        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    def _add_changes_action(self, name, callback) -> Gio.SimpleAction:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self._changes_action_group.add_action(action)
        return action

    def start_update(self):
        if self.update_in_progress:
            self.update_in_progress.clean_from_started_processes()
        update = self.update_class(directory=self.git_directory)
        update.start()
        self.show_update(update=update)

    def show_update(self, update: GitUpdate):
        update_view = MultistageProcessExecutionView()
        update_view.set_multistage_process(multistage_process=update)
        self.content_navigation_view.push_view(update_view, title="Updating Git repository")

