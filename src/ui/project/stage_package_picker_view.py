from __future__ import annotations

import os
import threading

from gi.repository import Gtk, Adw, GLib

from .project_build import StageBuild, StageBuildStatus, load_project_builds
from .project_stage_arguments import StageArgumentDetails
from .project_stage_value_resolver import UNRESOLVED, resolve_stage_argument
from .repository import Repository
from .project_stage_automatic_option import StageAutomaticOption
from .scrolled_window import scrolled_window
from .package_search import filter_package_categories, package_version_key


class StagePackagePickerView(Gtk.Box):
    """Browse package trees from the stage's Gentoo snapshot and enabled overlays."""

    def __init__(self, project, stage, argument, own_packages: list[str], on_change,
                 selected_source_options=()):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.project = project
        self.stage = stage
        self.argument = argument
        self.own_packages = list(own_packages or [])
        self.on_change = on_change
        self.selected_source_options = list(selected_source_options)
        self._repo_views = []
        self._repo_views_by_name = {}
        self.inherited_packages = self._inherited_package_lists()
        self._append_header()
        self.stack = Adw.ViewStack(vexpand=True)
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE,
                                    margin_start=24, margin_end=24)
        self.append(switcher)
        self.append(self.stack)
        self._add_repository_pages()
        self.stack.connect("notify::visible-child-name", self._page_changed)
        self._page_changed(self.stack, None)

    def _append_header(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                      margin_start=24, margin_end=24, margin_top=16)
        box.append(Gtk.Label(label="Select packages by category, then choose a version if needed. Unversioned entries use the best available version.",
                             xalign=0, wrap=True))
        box.append(Gtk.Label(label=f"{len(self.own_packages)} custom entr{'y' if len(self.own_packages) == 1 else 'ies'} selected",
                             xalign=0, css_classes=["dimmed", "caption"]))
        self.selection_summary = box.get_last_child()
        self.append(box)

    def _inherited_package_lists(self):
        argument = self.argument.details
        parent = resolve_stage_argument(self.project, self.stage, argument.value,
                                        option=StageAutomaticOption.INHERIT_FROM_PARENT)
        releng = resolve_stage_argument(self.project, self.stage, argument.value,
                                        option=StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE)
        return _as_list(parent), _as_list(releng), self._parent_installed_packages()

    def _parent_installed_packages(self):
        builds = load_project_builds(self.project)
        result = set()
        parent_id = getattr(self.stage, StageArgumentDetails.parent.name, None)
        visited = set()
        while parent_id and parent_id not in visited:
            visited.add(parent_id)
            parent = next((item for item in self.project.stages if item.id == parent_id), None)
            if parent is None:
                break
            build = next((item for item in builds if item.stage_id == parent.id
                          and item.status == StageBuildStatus.COMPLETED and item.path
                          and os.path.isfile(os.path.join(item.path, StageBuild.PACKAGES_FILE))), None)
            if build:
                try:
                    with open(os.path.join(build.path, StageBuild.PACKAGES_FILE), encoding="utf-8") as file:
                        result.update(line.strip() for line in file if line.strip())
                except OSError:
                    pass
            parent_id = getattr(parent, StageArgumentDetails.parent.name, None)
        return sorted(result)

    def _add_repository_pages(self):
        snapshot = self.project.get_snapshot()
        if snapshot:
            self._add_repository_page("Snapshot", "gentoo", snapshot.load_ebuilds)
        repos = resolve_stage_argument(self.project, self.stage, StageArgumentDetails.repos.value)
        repo_ids = repos if isinstance(repos, list) else []
        for overlay in Repository.OverlayDirectory.value:
            if overlay.id in repo_ids:
                self._add_repository_page(overlay.name, overlay.repo_name(), overlay.load_ebuilds)
        if not self._repo_views:
            placeholder = Adw.StatusPage(title="No package repositories", description="Select a snapshot or overlay for this stage.",
                                         icon_name="archive-minimalistic-svgrepo-com-symbolic")
            self.stack.add_titled(placeholder, "empty", "Repositories")

    def _add_repository_page(self, title, repo_name, loader):
        inherited = self._selected_inherited_package_lists()
        view = _SelectableRepositoryView(loader, repo_name, self.own_packages,
                                         inherited, self._set_own_packages)
        self._repo_views.append(view)
        page_name = f"repo-{len(self._repo_views)}"
        self._repo_views_by_name[page_name] = view
        self.stack.add_titled(scrolled_window(view), page_name, title)

    def _page_changed(self, stack, _):
        if view := self._repo_views_by_name.get(stack.get_visible_child_name()):
            view.load()

    def _set_own_packages(self, value):
        self.own_packages = value
        self.selection_summary.set_label(f"{len(value)} custom entr{'y' if len(value) == 1 else 'ies'} selected")
        for view in self._repo_views:
            view.set_selected(value)
        # Package and version edits are saved as they happen; there is no separate Apply step.
        self.on_change(self.own_packages, self.selected_source_options)

    def _selected_inherited_package_lists(self):
        parent, releng, installed = self.inherited_packages
        active = set(self.selected_source_options)
        return (
            parent if StageAutomaticOption.INHERIT_FROM_PARENT in active else [],
            releng if StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE in active else [],
            installed,
        )


class _SelectableRepositoryView(Gtk.Box):
    def __init__(self, loader, repo_name, selected, inherited, on_change):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         margin_start=24, margin_end=24, margin_top=16, margin_bottom=24)
        self.loader = loader
        self.repo_name = repo_name
        self.selected = list(selected)
        self.parent_packages, self.releng_packages, self.installed_parent_packages = inherited
        self.on_change = on_change
        self.rows = []
        self.filtered = []
        self._syncing = False
        self.search = Gtk.SearchEntry(placeholder_text="Search packages…", sensitive=False)
        self.search.connect("search-changed", self._filter)
        self.append(self.search)
        self.group = Adw.PreferencesGroup(title=repo_name)
        self.append(self.group)
        self.status = Adw.StatusPage(title="Reading packages…", icon_name="system-run-symbolic")
        self.append(self.status)
        self.loaded = False

    def load(self):
        if self.loaded:
            return
        self.loaded = True
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            packages = self.loader()
            error = None
        except Exception as exc:
            packages, error = {}, str(exc)
        GLib.idle_add(self._show, packages, error)

    def _show(self, packages, error):
        if error:
            self.status.set_title("Couldn't read packages")
            self.status.set_description(error)
            return False
        for category, names in packages.items():
            row = Adw.ExpanderRow(title=category)
            row.category = category
            row.packages = names
            row.packages_filtered = names
            row.children = []
            row.connect("notify::expanded", self._expand_category)
            self.rows.append(row)
        self.filtered = self.rows
        self._display()
        self.search.set_sensitive(True)
        self.status.set_visible(not self.rows)
        self.status.set_title("No packages")
        self.status.set_description("This repository contains no readable ebuilds.")
        return False

    def _expand_category(self, row, _):
        if row.get_expanded():
            row.children.clear()
            for package, versions in row.packages_filtered.items():
                row_widget = self._package_row(row.category, package, sorted(versions, key=package_version_key))
                row.add_row(row_widget)
                row.children.append(row_widget)
        else:
            for child in row.children:
                row.remove(child)
            row.children.clear()

    def _package_row(self, category, package, versions):
        base = f"{category}/{package}"
        row = Adw.ActionRow(title=package)
        sources = []
        if any(_atom_matches(item, base) for item in self.parent_packages):
            sources.append("Included by parent")
        if any(_atom_matches(item, base) for item in self.releng_packages):
            sources.append("Included by releng template")
        if any(_atom_matches(item, base) for item in self.installed_parent_packages):
            sources.append("Installed in parent build chain")
        version_text = ", ".join(versions[:6]) + (f" (+{len(versions) - 6})" if len(versions) > 6 else "")
        row.set_subtitle(" · ".join(([version_text] if version_text else []) + sources))
        own = [item for item in self.selected if _atom_matches(item, base)]
        inherited = bool(sources)
        check = Gtk.CheckButton(active=bool(own or inherited), valign=Gtk.Align.CENTER)
        check.set_sensitive(bool(own) or not inherited)
        check.connect("toggled", self._toggle, base, versions, check)
        row.add_prefix(check)
        row.set_activatable_widget(check)
        options = ["Any version", *versions]
        dropdown = Gtk.DropDown(model=Gtk.StringList.new(options), valign=Gtk.Align.CENTER)
        dropdown.set_enable_search(True)
        # Let the user choose a version before checking the package. Choosing a version
        # also selects it; packages already inherited don't need a duplicate atom.
        dropdown.set_sensitive(bool(own) or not inherited)
        selected_version = _selected_version_for_repo(own, base, self.repo_name)
        if selected_version in versions:
            dropdown.set_selected(versions.index(selected_version) + 1)
        dropdown.connect("notify::selected", self._version_changed, base, dropdown, check)
        row.add_suffix(dropdown)
        row.version_dropdown = dropdown
        row.package_check = check
        row.package_base = base
        row.package_versions = versions
        check.package_versions = versions
        return row

    def _toggle(self, check, base, versions, button):
        if self._syncing:
            return
        own = [item for item in self.selected if not _atom_matches(item, base)]
        if button.get_active():
            own.append(base)
        self._update(own)

    def _version_changed(self, dropdown, _param, base, selector, check):
        if self._syncing:
            return
        index = selector.get_selected()
        if index > len(check.package_versions):
            return
        atom = base if index == 0 else f"={base}-{check.package_versions[index - 1]}::{self.repo_name}"
        own = [item for item in self.selected if not _atom_matches(item, base)]
        own.append(atom)
        # Choosing a version is also a package selection, including when the checkbox was
        # previously clear. Update it under the sync guard so its toggle handler can't
        # replace the exact version atom with the unversioned package name.
        self._syncing = True
        check.set_active(True)
        self._syncing = False
        self._update(own)

    def _update(self, value):
        self.selected = value
        self.on_change(value)

    def set_selected(self, value):
        self.selected = list(value)
        for category in self.rows:
            if not category.get_expanded():
                continue
            for row in category.children:
                own = [item for item in self.selected if _atom_matches(item, row.package_base)]
                inherited = any(_atom_matches(item, row.package_base) for item in (
                    self.parent_packages + self.releng_packages + self.installed_parent_packages
                ))
                self._syncing = True
                row.package_check.set_active(bool(own or inherited))
                row.package_check.set_sensitive(bool(own) or not inherited)
                row.version_dropdown.set_sensitive(bool(own) or not inherited)
                selected_version = _selected_version_for_repo(own, row.package_base, self.repo_name)
                if selected_version in row.package_versions:
                    row.version_dropdown.set_selected(row.package_versions.index(selected_version) + 1)
                else:
                    row.version_dropdown.set_selected(0)
                self._syncing = False

    def set_inherited(self, inherited):
        self.parent_packages, self.releng_packages, self.installed_parent_packages = inherited
        self.set_selected(self.selected)

    def _filter(self, entry):
        query = entry.get_text()
        for row in self.filtered:
            self.group.remove(row)
        for row in self.rows:
            row.set_expanded(False)
        query = query.strip().casefold()
        categories = filter_package_categories(
            {row.category: row.packages for row in self.rows}, query, minimum=2)
        for row in self.rows:
            row.packages_filtered = categories.get(row.category, {})
        self.filtered = [row for row in self.rows if row.category in categories]
        if len(query) >= 2:
            for row in self.filtered:
                row.set_expanded(True)
        self._display()

    def _display(self):
        for row in self.filtered:
            self.group.add(row)
        self.status.set_visible(not self.filtered)


def _as_list(value):
    if value is UNRESOLVED or value is None:
        return []
    return value if isinstance(value, list) else [str(value)]


def _atom_matches(atom: str, package: str) -> bool:
    atom = str(atom).lstrip("<>=~!")
    atom = atom.split("::", 1)[0].split("[", 1)[0]
    atom = atom.split(":", 1)[0]
    if atom == package:
        return True
    suffix = atom[len(package):] if atom.startswith(package) else ""
    return len(suffix) > 1 and suffix[0] == "-" and suffix[1].isdigit()


def _selected_version_for_repo(atoms, base, repo_name):
    for atom in atoms:
        value = str(atom)
        if not value.startswith("="):
            continue
        repo = value.split("::", 1)[1] if "::" in value else None
        if repo and repo != repo_name:
            continue
        value = value.lstrip("<>=~!")
        if "::" in value:
            value = value.split("::", 1)[0]
        if ":" in value:
            value = value.split(":", 1)[0]
        if value.startswith(base + "-"):
            return value[len(base) + 1:]
    return None
