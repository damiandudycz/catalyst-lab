from gi.repository import Gtk, Adw
from .app_section import app_section
from .overlay_installation import OverlayInstallation
from .repository import Repository
from .status_indicator import items_status, processes_status
from .overlay_create_view import OverlayCreateView
from .overlay_update import OverlayUpdate
from .app_events import app_event_bus, AppEvents
from .overlay_details_view import OverlayDetailsView

@app_section(title="Overlays", icon="layers-minimalistic-svgrepo-com-symbolic", order=6_000)
@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/app_sections/overlays/overlays_section.ui')
class OverlaysSection(Gtk.Box):
    __gtype_name__ = "OverlaysSection"

    @staticmethod
    def section_status():
        """Unsaved changes of overlays, clones or updates running (side menu)."""
        return (items_status(Repository.OverlayDirectory.value) + processes_status(OverlayInstallation, OverlayUpdate))

    def __init__(self, content_navigation_view: Adw.NavigationView, **kwargs):
        super().__init__(**kwargs)
        self.content_navigation_view = content_navigation_view

    @Gtk.Template.Callback()
    def on_item_row_pressed(self, sender, item):
        view = OverlayDetailsView(overlay=item, content_navigation_view=self.content_navigation_view)
        self.content_navigation_view.push_view(view, title="Portage overlay details")

    @Gtk.Template.Callback()
    def on_installation_row_pressed(self, sender, installation):
        app_event_bus.emit(AppEvents.PRESENT_VIEW, OverlayCreateView(installation_in_progress=installation), "New portage overlay", 640, 480)

    @Gtk.Template.Callback()
    def on_add_new_item_pressed(self, sender):
        app_event_bus.emit(AppEvents.PRESENT_VIEW, OverlayCreateView(), "New portage overlay", 640, 480)

