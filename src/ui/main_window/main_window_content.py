from gi.repository import Gtk, GObject, Adw
from functools import partial
from .app_section import AppSection
from .root_access_button import RootAccessButton

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/main_window/main_window_content.ui')
class CatalystlabWindowContent(Gtk.Box):
    """Wrapper container used to display the main content."""
    __gtype_name__ = 'CatalystlabWindowContent'

    __gsignals__ = {
        'toggle-sidebar': (GObject.SignalFlags.RUN_FIRST, None, (Gtk.Button,))
    }

    # View elements:
    content = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sidebar_toggle_button = None
        self.sidebar_toggle_button_visible = False

    def open_app_section(self, section: AppSection):
        """Load content of selected main section."""
        navigation_view = Adw.NavigationView()
        navigation_view.sidebar_toggle_button_visible = self.sidebar_toggle_button_visible

        section_view = section(content_navigation_view=navigation_view)

        header = Adw.HeaderBar()

        toolbar_view = Adw.ToolbarView()
        toolbar_view.set_content(section_view)
        toolbar_view.add_top_bar(header)

        page = Adw.NavigationPage.new(toolbar_view, section.section_details.title)
        navigation_view.push(page)

        # Add the root access button to the header
        root_access_button = RootAccessButton()
        header.pack_end(root_access_button)

        self.sidebar_toggle_button = Gtk.Button.new()
        self.sidebar_toggle_button.set_icon_name("sidebar-show-symbolic")
        self.sidebar_toggle_button.set_tooltip_text("Toggle sidebar")
        self.sidebar_toggle_button.set_visible(self.sidebar_toggle_button_visible)
        self.sidebar_toggle_button.connect("clicked", partial(self.emit, "toggle-sidebar"))

        header.pack_start(self.sidebar_toggle_button)
        self.replace_content(navigation_view)

    def replace_content(self, new_widget: Gtk.Widget):
        """Replace the current content with a new widget."""
        self.remove(self.content)
        self.append(new_widget)
        self.content = new_widget

    @property
    def sidebar_toggle_button_visible(self) -> bool:
        """Return the last set visibility value (not actual widget state)."""
        return self._sidebar_toggle_button_visible
    @sidebar_toggle_button_visible.setter
    def sidebar_toggle_button_visible(self, visible: bool):
        """Set the visibility of the sidebar toggle button, if it exists."""
        self._sidebar_toggle_button_visible = visible
        if self.sidebar_toggle_button is not None:
            self.sidebar_toggle_button.set_visible(visible)
