from gi.repository import Gtk, Adw
from .git_directory_details_view import GitDirectoryDetailsView
from .releng_manager import RelengManager
from .releng_update import RelengUpdate
from .files_browser_view import FilesBrowserView
from .scrolled_window import scrolled_window

class RelengDetailsView(Gtk.Box):
    """Releng directory: its Git directory (status, updates, changes) and its files (read-only), in tabs like overlay
    details. Files are read when their tab is opened first time."""

    def __init__(self, releng_directory, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.releng_directory = releng_directory
        self.content_navigation_view = content_navigation_view
        self.stack = Adw.ViewStack(vexpand=True)
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE, margin_start=24, margin_end=24,
                                    margin_bottom=12)
        self.append(switcher)
        self.append(self.stack)
        # Releng (Git directory):
        details = GitDirectoryDetailsView(git_directory=releng_directory, manager_class=RelengManager, update_class=RelengUpdate)
        details.content_navigation_view = content_navigation_view
        details.set_margin_start(24)
        details.set_margin_end(24)
        details.set_margin_bottom(24)
        self.stack.add_titled_with_icon(scrolled_window(details), "releng", "Releng", "book-minimalistic-svgrepo-com-symbolic")
        # Files:
        self.browser = FilesBrowserView(root=releng_directory.directory_path(), content_navigation_view=content_navigation_view)
        self.browser.set_margin_start(24)
        self.browser.set_margin_end(24)
        self.browser.set_margin_bottom(24)
        self.stack.add_titled_with_icon(scrolled_window(self.browser), "files", "Files", "folder-symbolic")
        self.stack.connect("notify::visible-child-name", self._on_page_changed)

    def _on_page_changed(self, stack, _param):
        if stack.get_visible_child_name() == "files":
            # Read again when tab is opened, files change with updates.
            self.browser.load(force=True)
