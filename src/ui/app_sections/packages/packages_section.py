from gi.repository import Gtk, Adw
from .app_section import app_section
from .repository_list_view import RepositoryListView
from .status_indicator import items_status
from .packages_views import PackagesDirectoryDetailsView, present_new_packages_directory_dialog

@app_section(title="Binary packages", label="Packages", icon="archive-minimalistic-svgrepo-com-symbolic", order=2_700)
class PackagesSection(Gtk.Box):
    """Binary packages folders, shared by projects."""
    __gtype_name__ = "PackagesSection"

    @staticmethod
    def section_status():
        """Folders used by running builds (side menu)."""
        from .repository import Repository
        return items_status(Repository.PackagesDirectory.value)

    def __init__(self, content_navigation_view: Adw.NavigationView, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, **kwargs)
        self.content_navigation_view = content_navigation_view
        scrolled_window = Gtk.ScrolledWindow(hexpand=True, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_start=24, margin_end=24, margin_bottom=24)
        scrolled_window.set_child(content)
        self.append(scrolled_window)
        self.list_view = RepositoryListView()
        for name, value in (("title", "Binary packages folders"), ("item_class_name", "PackagesDirectory"),
                            ("add_button_title", "Add folder"), ("item_icon", "archive-minimalistic-svgrepo-com-symbolic"),
                            ("item_title_property_name", "name"), ("item_subtitle_property_name", "short_details"),
                            ("item_status_property_name", "status_indicator_values"), ("show_installations", False)):
            self.list_view.set_property(name, value)
        self.list_view.connect("item-row-pressed", self._on_item_row_pressed)
        self.list_view.connect("add-new-item-pressed", self._on_add_pressed)
        content.append(self.list_view)
        content.append(Gtk.Separator())
        description = Gtk.Label(wrap=True, justify=Gtk.Justification.FILL, halign=Gtk.Align.CENTER, label=(
            "Binary packages built with stages are kept in these folders and reused by next builds. Projects with the "
            "same architecture and CPU flags can share a folder, to reuse packages built by each other."))
        description.add_css_class("dimmed")
        content.append(description)

    def _on_item_row_pressed(self, sender, item):
        self.content_navigation_view.push_view(PackagesDirectoryDetailsView(item, self.content_navigation_view), title=item.name)

    def _on_add_pressed(self, sender):
        present_new_packages_directory_dialog(self)
