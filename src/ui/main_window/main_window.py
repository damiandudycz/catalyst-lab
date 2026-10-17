from gi.repository import Adw, Gtk
from .app_section import AppSection
from .app_events import AppEvents, app_event_bus
from . import navigation_view_extensions as _navigation_view_extensions

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/main_window/main_window.ui')
class CatalystlabWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'CatalystlabWindow'

    # Static config:
    allow_side_menu_toggle = True # Only for sections that allows that at all.

    # View elements:
    navigation_view = Gtk.Template.Child() # Main full window navigation_view
    split_view = Gtk.Template.Child()
    content_view = Gtk.Template.Child()
    side_menu = Gtk.Template.Child()
    sidebar_toggle_breakpoint = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        app_event_bus.subscribe(AppEvents.OPEN_APP_SECTION, self.opened_app_section)
        app_event_bus.subscribe(AppEvents.PUSH_VIEW, self.navigation_view.push_view)
        app_event_bus.subscribe(AppEvents.PUSH_SECTION, self.navigation_view.push_section)
        app_event_bus.subscribe(AppEvents.PRESENT_VIEW, self._present_view)
        app_event_bus.subscribe(AppEvents.PRESENT_SECTION, self._present_section)
        # Load initial section page:
        app_event_bus.emit(AppEvents.OPEN_APP_SECTION, AppSection.WelcomeSection)
        # Connect sidebar_toggle_breakpoint actions
        self.sidebar_toggle_breakpoint.connect("apply", self._on_sidebar_toggle_breakpoint_apply)
        self.sidebar_toggle_breakpoint.connect("unapply", self._on_sidebar_toggle_breakpoint_unapply)
        # Observe AppEvents.TOGGLE_SIDEBAR
        app_event_bus.subscribe(AppEvents.TOGGLE_SIDEBAR, self.toggle_sidebar)
        # Files of Git directories can change outside of app (other editors, Finder), their statuses are read again
        # when window becomes active.
        self.connect("notify::is-active", self._on_active_changed)
        self.connect("close-request", self._on_close_request)

    def _on_close_request(self, _window):
        from .shutdown_dialog import request_shutdown
        return request_shutdown(self)

    def _on_active_changed(self, window, param):
        if not self.is_active():
            return
        from .repository import Repository
        for repository in (Repository.ProjectDirectory, Repository.RelengDirectory, Repository.OverlayDirectory):
            for directory in repository.value:
                directory.update_status()

    def toggle_sidebar(self, _ = None):
        if self.allow_side_menu:
            self.split_view.set_show_sidebar(not self.split_view.get_show_sidebar())

    # Toggle sidebar visibility with button.
    @Gtk.Template.Callback()
    def sidebar_toggle_button_clicked(self, window_content, button):
        """Callback function that is called when we click the button"""
        self.toggle_sidebar()

    # Managing side bar and toggle side bar button visibility for selected section and collapsed state:

    def opened_app_section(self, section: AppSection):
        self.allow_side_menu = section.section_details.show_side_bar
        self.split_view.set_show_sidebar(self.allow_side_menu and not self.split_view.get_collapsed())
        self.content_view.sidebar_toggle_button_visible = self.allow_side_menu and ( self.split_view.get_collapsed() or CatalystlabWindow.allow_side_menu_toggle )
        self.content_view.open_app_section(section)
        self.navigation_view.pop_to_tag("root")

    def _on_sidebar_toggle_breakpoint_apply(self, breakpoint):
        self.split_view.set_collapsed(True)
        self.split_view.set_show_sidebar(False)
        self.content_view.sidebar_toggle_button_visible = self.allow_side_menu

    def _on_sidebar_toggle_breakpoint_unapply(self, breakpoint):
        self.split_view.set_collapsed(False)
        self.split_view.set_show_sidebar(self.allow_side_menu)
        self.content_view.sidebar_toggle_button_visible = self.allow_side_menu and CatalystlabWindow.allow_side_menu_toggle

    def _present_section(self, section: AppSection, **kwargs):
        view = section(content_navigation_view=None, **kwargs)
        title = section.section_details.title
        self._present_view(view, title, 640, 480)

    def _present_view(self, view: Gtk.Widget, title: str, width: int = -1, height: int = -1):
        """Shows view (wizard, installation...) in window over main window, or pushed in main window, depending on
        navigation style from settings. View closes itself with view._window.close() in both cases."""
        from .repository import Repository
        from .settings import NavigationStyle
        if Repository.Settings.value.navigation_style == NavigationStyle.PUSH:
            # Set before pushing, views pass it to their subviews when realized.
            view._window = PushedViewHandle(self.navigation_view, view)
            self.navigation_view.push_view(view, title)
            if (wizard := getattr(view, "wizard_view", None)) is not None:
                _connect_wizard_back(view._page, view._header, wizard)
            return
        header = Adw.HeaderBar()
        for widget in getattr(view, "header_bar_end_widgets", lambda: [])():
            header.pack_end(widget)
        toolbar_view = Adw.ToolbarView()
        # Container centers the content.
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        container.append(view)
        toolbar_view.set_content(container)
        toolbar_view.add_top_bar(header)
        window = Adw.Window()
        window.set_transient_for(self.get_root())
        window.set_modal(True)
        window.set_title(title)
        if hasattr(view, "content_navigation_view"):
            # If view supports content_navigation_view (is section) embed it into new nav_view
            nav_view = Adw.NavigationView()
            nav_view.show_root_access_button = False # Views pushed in dialogs don't show root access button.
            view.content_navigation_view = nav_view
            page = Adw.NavigationPage.new(toolbar_view, title)
            nav_view.push(page)
            window.set_content(nav_view)
        else:
            window.set_content(toolbar_view)
        window.set_default_size(width, height)
        view._window = window
        window.present()

def _connect_wizard_back(page: Adw.NavigationPage, header: Adw.HeaderBar, wizard):
    """Back of pushed wizard goes to its previous page, until first page (or installation) is shown. Navigation can't
    go back then (back button, Escape, swipe), back button of wizard is shown instead."""
    back_button = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back", visible=False)
    back_button.connect("clicked", lambda button: wizard.go_back())
    header.pack_start(back_button)
    shortcuts = Gtk.ShortcutController(scope=Gtk.ShortcutScope.LOCAL)
    action = Gtk.CallbackAction.new(lambda *args: wizard.go_back() or True)
    shortcuts.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Escape|<alt>Left"), action))
    page.add_controller(shortcuts)
    def update(*args):
        page.set_can_pop(not wizard.can_go_back)
        back_button.set_visible(wizard.can_go_back)
        shortcuts.set_propagation_phase(Gtk.PropagationPhase.BUBBLE if wizard.can_go_back else Gtk.PropagationPhase.NONE)
    wizard.connect("notify::can-go-back", update)
    update()

class PushedViewHandle:
    """Used by view pushed in main window instead of window it would be presented in: closing goes back to page
    shown before view (also when view pushed more pages)."""

    def __init__(self, navigation_view: Adw.NavigationView, view: Gtk.Widget):
        self.navigation_view = navigation_view
        self.view = view

    def close(self):
        page = getattr(self.view, "_page", None)
        if page is None:
            return
        stack = self.navigation_view.get_navigation_stack()
        if page not in [stack.get_item(index) for index in range(stack.get_n_items())]:
            return # Already removed (eg. went back).
        if (previous := self.navigation_view.get_previous_page(page)) is not None:
            self.navigation_view.pop_to_page(previous)
