from gi.repository import Gtk, Adw
from .git_directory_details_view import GitDirectoryDetailsView
from .overlay_manager import OverlayManager
from .overlay_update import OverlayUpdate
from .packages_browser_view import PackagesBrowserView

class OverlayDetailsView(Gtk.Box):
    """Portage overlay: its Git directory (status, updates, changes) and packages, in tabs like project details.
    Packages are read when their tab is opened first time."""

    def __init__(self, overlay, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.overlay = overlay
        self.content_navigation_view = content_navigation_view
        self.stack = Adw.ViewStack(vexpand=True)
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE, margin_start=24, margin_end=24,
                                    margin_bottom=12)
        self.append(switcher)
        self.append(self.stack)
        # Overlay (Git directory):
        details = GitDirectoryDetailsView(git_directory=overlay, manager_class=OverlayManager, update_class=OverlayUpdate)
        details.content_navigation_view = content_navigation_view
        details.set_margin_start(24)
        details.set_margin_end(24)
        details.set_margin_bottom(24)
        self.stack.add_titled_with_icon(self._scrolled(details), "overlay", "Overlay", "layers-minimalistic-svgrepo-com-symbolic")
        # Packages:
        self.browser = PackagesBrowserView(loader=overlay.load_ebuilds, loading_title="Reading packages…")
        self.browser.set_margin_start(24)
        self.browser.set_margin_end(24)
        self.browser.set_margin_bottom(24)
        self.stack.add_titled_with_icon(self._scrolled(self.browser), "packages", "Packages", "archive-minimalistic-svgrepo-com-symbolic")
        self.stack.connect("notify::visible-child-name", self._on_page_changed)

    @staticmethod
    def _scrolled(child: Gtk.Widget) -> Gtk.ScrolledWindow:
        return Gtk.ScrolledWindow(child=child, hexpand=True, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _on_page_changed(self, stack, _param):
        if stack.get_visible_child_name() == "packages":
            self.browser.load()
