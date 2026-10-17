from __future__ import annotations
import os, subprocess, sys, threading
from gi.repository import Gtk, GLib, Adw
from .combo_row_popup import fit_popup_to_items
from .repository import Repository, RepositoryEvent
from .architecture import Architecture
from .status_indicator import StatusIndicatorState
from catalystlab.formatters import format_size
from .packages_directory import (
    PackagesDirectory, create_packages_directory, delete_packages_directory, is_packages_name_available, unique_packages_name
)

def open_folder(path: str):
    """Opens folder in file manager."""
    os.makedirs(path, exist_ok=True)
    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def present_new_packages_directory_dialog(parent: Gtk.Widget, architecture: Architecture | None = None,
                                          cpu_flags: str | None = None, name: str = "", on_created=None):
    """Dialog creating binary packages folder: name and architecture."""
    name = unique_packages_name(name) if name else name
    name_row = Adw.EntryRow(title="Name", text=name)
    architectures = sorted(Architecture, key=lambda item: item.name)
    architecture_row = fit_popup_to_items(Adw.ComboRow(title="Architecture", model=Gtk.StringList.new([item.name for item in architectures])))
    if architecture in architectures:
        architecture_row.set_selected(architectures.index(architecture))
    list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    list_box.add_css_class("boxed-list")
    list_box.append(name_row)
    list_box.append(architecture_row)
    dialog = Adw.AlertDialog(heading="New binary packages folder", extra_child=list_box,
                             body="Packages built by projects using this folder are kept in it and reused by them.")
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("create", "Create")
    dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("create")
    dialog.set_close_response("cancel")
    def update_enabled(*args):
        dialog.set_response_enabled("create", is_packages_name_available(name_row.get_text()))
    name_row.connect("changed", update_enabled)
    update_enabled()
    def on_response(dialog, response):
        if response != "create":
            return
        directory = create_packages_directory(name_row.get_text().strip(), architectures[architecture_row.get_selected()], cpu_flags)
        if on_created:
            on_created(directory)
    dialog.connect("response", on_response)
    dialog.present(parent.get_root())

class PackagesDirectoryDetailsView(Gtk.Box):
    """Binary packages folder: its architecture, projects using it, packages by release types, location."""

    def __init__(self, directory: PackagesDirectory, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.directory = directory
        self.content_navigation_view = content_navigation_view
        scrolled_window = Gtk.ScrolledWindow(hexpand=True, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_start=24, margin_end=24,
                               margin_top=6, margin_bottom=24)
        scrolled_window.set_child(self.content)
        self.append(scrolled_window)
        self._build()
        Repository.ProjectDirectory.event_bus.subscribe(RepositoryEvent.VALUE_CHANGED, self._on_projects_changed)

    def _on_projects_changed(self, *args):
        self._build()

    def _build(self):
        while child := self.content.get_first_child():
            self.content.remove(child)
        directory = self.directory
        # Information:
        info = Adw.PreferencesGroup(title="Binary packages folder")
        info.add(Adw.ActionRow(title="Architecture", subtitle=directory.architecture.name if directory.architecture else "Any"))
        info.add(Adw.ActionRow(title="CPU flags", subtitle=GLib.markup_escape_text(directory.cpu_flags or "Defaults of architecture")))
        location = Adw.ActionRow(title="Location", subtitle=GLib.markup_escape_text(directory.directory_path()))
        location.set_subtitle_selectable(True)
        open_button = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Open folder", valign=Gtk.Align.CENTER)
        open_button.add_css_class("flat")
        open_button.connect("clicked", lambda button: open_folder(directory.directory_path()))
        location.add_suffix(open_button)
        info.add(location)
        self.size_row = Adw.ActionRow(title="Size", subtitle="Calculating…")
        info.add(self.size_row)
        self.content.append(info)
        # Projects:
        projects = Adw.PreferencesGroup(title="Projects", description="Projects using this folder reuse packages built by each other.")
        for project in directory.projects:
            row = Adw.ActionRow(title=GLib.markup_escape_text(project.name), icon_name="notes-minimalistic-svgrepo-com-symbolic")
            problems = directory.compatibility(project)
            if problems:
                # Like wrong values in stage details: highlighted row, details of problems in tooltip of icon.
                error = any(problem.state == StatusIndicatorState.ERROR for problem in problems)
                style = "error" if error else "warning"
                row.add_css_class(style)
                row.set_subtitle("Packages can't be used by this project" if error else
                                 f"Packages might not run on machines of this project ({len(problems)} "
                                 f"{'issue' if len(problems) == 1 else 'issues'})")
                icon = Gtk.Image.new_from_icon_name("danger-triangle-svgrepo-com-symbolic")
                icon.add_css_class(style)
                icon.set_tooltip_text("\n\n".join(problem.text for problem in problems))
                row.add_suffix(icon)
            projects.add(row)
        if not directory.projects:
            projects.add(Adw.ActionRow(title="Not used by any project"))
        self.content.append(projects)
        # Packages by release types (rel_type subfolders):
        self.contents = Adw.PreferencesGroup(title="Packages", description="Packages of stages are kept by release type of stages.")
        self.content.append(self.contents)
        # Delete:
        delete_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        delete_list.add_css_class("boxed-list")
        delete_row = Adw.ButtonRow(title="Delete folder", start_icon_name="trash-bin-trash-svgrepo-com-symbolic")
        delete_row.add_css_class("destructive-action")
        delete_row.connect("activated", self._on_delete)
        delete_list.append(delete_row)
        self.content.append(delete_list)
        threading.Thread(target=self._measure, daemon=True).start()

    def _measure(self):
        root = self.directory.directory_path()
        total = self.directory.size()
        release_types = []
        if os.path.isdir(root):
            for directory, directories, files in os.walk(root):
                # Release types are folders with packages (Packages index of binary repository).
                if "Packages" in files:
                    size = sum(os.lstat(os.path.join(path, name)).st_size for path, _, names in os.walk(directory) for name in names)
                    release_types.append((os.path.relpath(directory, root), size))
                    directories[:] = []
        def show():
            self.size_row.set_subtitle(format_size(total))
            for release_type, size in sorted(release_types):
                self.contents.add(Adw.ActionRow(title=GLib.markup_escape_text(release_type), subtitle=format_size(size)))
            if not release_types:
                self.contents.add(Adw.ActionRow(title="No packages yet"))
            return False
        GLib.idle_add(show)

    def _on_delete(self, row):
        projects = self.directory.projects
        body = "Packages in it are deleted."
        if projects:
            body += f" Projects {', '.join(project.name for project in projects)} won't have binary packages folder, packages are kept in their builds."
        dialog = Adw.AlertDialog(heading=f"Delete {self.directory.name}?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        def on_response(dialog, response):
            if response != "delete":
                return
            try:
                delete_packages_directory(self.directory)
            except Exception as error:
                error_dialog = Adw.AlertDialog(
                    heading="Binary packages couldn't be deleted",
                    body=f"The folder could not be removed: {error}",
                )
                error_dialog.add_response("close", "Close")
                error_dialog.present(self.get_root())
                return
            if self.content_navigation_view:
                self.content_navigation_view.pop()
        dialog.connect("response", on_response)
        dialog.present(self.get_root())
