from gi.repository import Gtk
from gi.repository import Adw
from .app_section import AppSection
from .root_access_button import RootAccessButton
from .app_events import AppEvents, app_event_bus
from functools import partial

def _push_section(self, section: AppSection, **kwargs):
    view = section(content_navigation_view=self, **kwargs)
    title = section.section_details.title
    self.push_view(view, title)

def _push_view(self, view: Gtk.Widget, title: str):
    # If dealing view view without set content_navigation_view set it to self.
    if not hasattr(view, "content_navigation_view") or view.content_navigation_view is None:
        view.content_navigation_view = self # TODO: Test this
    # Create a header bar
    header = Adw.HeaderBar()
    # Wrap the content in a ToolbarView with the header
    toolbar_view = Adw.ToolbarView()
    toolbar_view.set_content(view)
    toolbar_view.add_top_bar(header)
    # Add the root access button to the header
    # Root access button is shown only in main window, not in views pushed in presented windows (dialogs).
    # TODO: Make this optional depending on configuration.
    if getattr(self, "show_root_access_button", True):
        root_access_button = RootAccessButton()
        header.pack_end(root_access_button)
    # Widgets of view for header bar (eg. view options), placed before root access button.
    for widget in getattr(view, "header_bar_end_widgets", lambda: [])():
        header.pack_end(widget)
    # Create a navigation page with title and child
    page = Adw.NavigationPage.new(toolbar_view, title)
    view._page = page # TODO: Check if this needs weakref
    view._header = header
    # Add sidebar toggle button to new presented view:
    if hasattr(self, 'sidebar_toggle_button_visible'):
        self.sidebar_toggle_button = Gtk.Button.new()
        self.sidebar_toggle_button.set_icon_name("sidebar-show-symbolic")
        self.sidebar_toggle_button.set_tooltip_text("Toggle sidebar")
        self.sidebar_toggle_button.set_visible(self.sidebar_toggle_button_visible)
        self.sidebar_toggle_button.connect("clicked", partial(app_event_bus.emit, AppEvents.TOGGLE_SIDEBAR))
        header.pack_start(self.sidebar_toggle_button)
    self.push(page)

Adw.NavigationView.push_section = _push_section
Adw.NavigationView.push_view = _push_view
