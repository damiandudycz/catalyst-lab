from gi.repository import Gtk, Adw
from .app_section import app_section
from .build_machine_installation import BuildMachineInstallation
from .toolset_update import ToolsetUpdate
from .toolset_installation import ToolsetInstallation
from .repository import Repository
from .status_indicator import items_status, processes_status
from .toolset_details_view import ToolsetDetailsView
from .toolset_create_view import ToolsetCreateView
from .app_events import app_event_bus, AppEvents
from .build_machine_create_view import BuildMachineCreateView
from .build_machine_details_view import BuildMachineDetailsView
from .lima import virtual_machines_supported

@app_section(title="Environments", icon="toolbox-symbolic", order=3_000)
@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/app_sections/environments/environments_section.ui')
class EnvironmentsSection(Gtk.Box):
    __gtype_name__ = "EnvironmentsSection"

    @staticmethod
    def section_status():
        """Running virtual machines, mounted or used toolsets, environments being created or updated (side menu)."""
        machines = Repository.BuildMachine.value if virtual_machines_supported() else []
        return (items_status(machines) + items_status(Repository.Toolset.value)
                               + processes_status(ToolsetInstallation, ToolsetUpdate, BuildMachineInstallation))

    machines_list = Gtk.Template.Child()
    machines_description_label = Gtk.Template.Child()

    def __init__(self, content_navigation_view: Adw.NavigationView, **kwargs):
        super().__init__(**kwargs)
        self.content_navigation_view = content_navigation_view
        # Virtual machines are used only where toolsets can't run on this computer (see virtual_machines_supported).
        self.machines_list.set_visible(virtual_machines_supported())
        self.machines_description_label.set_visible(virtual_machines_supported())

    @Gtk.Template.Callback()
    def on_item_row_pressed(self, sender, item):
        self.content_navigation_view.push_view(ToolsetDetailsView(toolset=item), title="Toolset details")

    @Gtk.Template.Callback()
    def on_installation_row_pressed(self, sender, installation):
        app_event_bus.emit(AppEvents.PRESENT_VIEW, ToolsetCreateView(installation_in_progress=installation), "New toolset", 640, 480)

    @Gtk.Template.Callback()
    def on_add_new_item_pressed(self, sender):
        app_event_bus.emit(AppEvents.PRESENT_VIEW, ToolsetCreateView(), "New toolset", 640, 480)

    @Gtk.Template.Callback()
    def on_machine_row_pressed(self, sender, item):
        self.content_navigation_view.push_view(BuildMachineDetailsView(machine=item, content_navigation_view=self.content_navigation_view), title="Virtual machine")

    @Gtk.Template.Callback()
    def on_machine_installation_row_pressed(self, sender, installation):
        app_event_bus.emit(AppEvents.PRESENT_VIEW, BuildMachineCreateView(installation_in_progress=installation), "New virtual machine", 640, 480)

    @Gtk.Template.Callback()
    def on_add_new_machine_pressed(self, sender):
        app_event_bus.emit(AppEvents.PRESENT_VIEW, BuildMachineCreateView(), "New virtual machine", 640, 480)
