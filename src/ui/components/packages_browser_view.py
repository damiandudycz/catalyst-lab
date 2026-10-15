from gi.repository import Gtk, Adw, GLib
import re, threading
from typing import Callable

# ------------------------------------------------------------------------------
# Browser of packages of ebuild repository (snapshot, overlay): categories with their packages, and search. Packages
# are listed by loader in background: {category: {package: [versions]}}.

class PackagesBrowserView(Gtk.Box):
    """Categories (expanded to list packages) of ebuild repository, searched by package or category/package."""

    def __init__(self, loader: Callable[[], dict[str, dict[str, list[str]]]], loading_title: str = "Loading packages…"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.loader = loader
        self.category_rows = []
        self.category_rows_filtered = []
        self.search_entry = Gtk.SearchEntry(hexpand=True, placeholder_text="Search packages...", sensitive=False)
        self.search_entry.connect("search-changed", self.on_search_changed)
        self.append(self.search_entry)
        self.packages_list = Adw.PreferencesGroup(title="Packages")
        self.append(self.packages_list)
        self.loading_page = Adw.StatusPage(title=loading_title, description="Please wait while packages are being read.",
                                           icon_name="system-run-symbolic")
        self.append(self.loading_page)
        self.no_results_page = Adw.StatusPage(title="No results", description="Try a different search term.",
                                              icon_name="edit-find-symbolic", visible=False)
        self.append(self.no_results_page)
        self.loaded = False

    def load(self):
        """Reads packages in background (once)."""
        if self.loaded:
            return
        self.loaded = True
        threading.Thread(target=self._load_packages, daemon=True).start()

    def _load_packages(self):
        try:
            packages_by_category = self.loader()
        except Exception as e:
            print(f"Failed to read packages: {e}")
            packages_by_category = {}
        GLib.idle_add(self._show_packages, packages_by_category)

    def _show_packages(self, packages_by_category):
        self.category_rows = []
        for category, packages in packages_by_category.items():
            category_row = Adw.ExpanderRow(title=category)
            category_row.category = category
            category_row.packages = packages
            category_row.packages_filtered = packages
            category_row.package_rows_shown = []
            category_row.connect("notify::expanded", self._on_category_expanded)
            self.category_rows.append(category_row)
        self.category_rows_filtered = self.category_rows
        self.display_rows()
        self.search_entry.set_sensitive(True)
        self.loading_page.set_visible(False)
        self.no_results_page.set_title("No results" if self.category_rows else "No packages")
        self.no_results_page.set_description("Try a different search term." if self.category_rows else "Repository has no ebuilds.")
        return False

    @staticmethod
    def _on_category_expanded(row, _param):
        # Package rows are created only for expanded categories (repositories have many packages).
        if row.get_expanded():
            for package, versions in row.packages_filtered.items():
                package_row = Adw.ActionRow(title=package, subtitle=", ".join(sorted(versions, key=_version_key)))
                package_row.package = package
                row.add_row(package_row)
                row.package_rows_shown.append(package_row)
        else:
            for child in row.package_rows_shown:
                row.remove(child)
            row.package_rows_shown.clear()

    def on_search_changed(self, entry):
        # Clear current rows first:
        for row in self.category_rows_filtered:
            self.packages_list.remove(row)
        for row in self.category_rows:
            row.set_expanded(False)
        search_text = entry.get_text().lower()
        if len(search_text) < 3: # Start search from 3 chars
            self.category_rows_filtered = self.category_rows
            for category_row in self.category_rows:
                category_row.packages_filtered = category_row.packages
            self.display_rows()
            return
        # Filter results:
        if "/" in search_text:
            search_full_name = True
            search_text_category, search_text_package = search_text.split("/", 1)
        else:
            search_full_name = False
            search_text_category = search_text
            search_text_package = search_text
        for row in self.category_rows:
            row.packages_filtered = {
                package: versions
                for package, versions in row.packages.items()
                if (
                    not search_full_name and search_text_package in package
                    or search_full_name and search_text in f"{row.category}/{package}"
                )
            }
        filtered_category_rows = []
        expanded_rows = []
        for row in self.category_rows:
            if row.packages_filtered:
                expanded_rows.append(row)
            if search_text_category in row.category and not search_full_name:
                row.packages_filtered = row.packages
            if row.packages_filtered:
                filtered_category_rows.append(row)
        self.category_rows_filtered = filtered_category_rows
        for row in expanded_rows:
            row.set_expanded(True)
        self.display_rows()

    def display_rows(self):
        for category_row in self.category_rows_filtered:
            self.packages_list.add(category_row)
        self.no_results_page.set_visible(not self.category_rows_filtered)

def _version_key(version: str) -> list:
    """Versions in order of their numbers (6.6.62 before 6.12.16)."""
    return [int(part) for part in re.findall(r"\d+", version)]
