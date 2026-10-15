from gi.repository import Gtk, Adw
from .app_section import app_section
from .releng_installation import RelengInstallation
from .repository import Repository
from .status_indicator import items_status, processes_status
from .releng_create_view import RelengCreateView
from .releng_manager import RelengManager
from .releng_update import RelengUpdate
from .app_events import app_event_bus, AppEvents
from .releng_details_view import RelengDetailsView

@app_section(title="Releng", icon="book-minimalistic-svgrepo-com-symbolic", order=4_000)
@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/app_sections/releng/releng_section.ui')
class RelengSection(Gtk.Box):
    __gtype_name__ = "RelengSection"

    @staticmethod
    def section_status():
        """Unsaved changes of releng directories, clones or updates running (side menu)."""
        return (items_status(Repository.RelengDirectory.value) + processes_status(RelengInstallation, RelengUpdate))

    def __init__(self, content_navigation_view: Adw.NavigationView, **kwargs):
        super().__init__(**kwargs)
        self.content_navigation_view = content_navigation_view

    @Gtk.Template.Callback()
    def on_item_row_pressed(self, sender, item):
        view = RelengDetailsView(releng_directory=item, content_navigation_view=self.content_navigation_view)
        self.content_navigation_view.push_view(view, title="Releng directory details")

    @Gtk.Template.Callback()
    def on_installation_row_pressed(self, sender, installation):
        app_event_bus.emit(AppEvents.PRESENT_VIEW, RelengCreateView(installation_in_progress=installation), "New releng directory", 640, 480)

    @Gtk.Template.Callback()
    def on_add_new_item_pressed(self, sender):
        app_event_bus.emit(AppEvents.PRESENT_VIEW, RelengCreateView(), "New releng directory", 640, 480)

