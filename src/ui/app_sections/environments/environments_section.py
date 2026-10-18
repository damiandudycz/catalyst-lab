from gi.repository import Gtk, Adw
from .app_section import app_section
from .build_machine_installation import VMRunnerInstallation
from .toolset_update import ToolsetUpdate
from .toolset_installation import ToolsetInstallation
from .repository import Repository
from .status_indicator import items_status, processes_status
from .toolset_details_view import ToolsetDetailsView
from .toolset_create_view import ToolsetCreateView
from .app_events import app_event_bus, AppEvents
from .build_machine_create_view import VMRunnerCreateView
from .build_machine_details_view import VMRunnerDetailsView
from .lima import virtual_machines_supported
from .runner import HOST_RUNNER
from .event_bus import SharedEvent

@app_section(title="Environments", icon="toolbox-symbolic", order=3_000)
@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/app_sections/environments/environments_section.ui')
class EnvironmentsSection(Gtk.Box):
    __gtype_name__ = "EnvironmentsSection"

    @staticmethod
    def section_status():
        """Running virtual machines, mounted or used toolsets, environments being created or updated (side menu)."""
        machines = Repository.Runner.value if virtual_machines_supported() else []
        return (items_status(machines) + items_status(Repository.Toolset.value)
                               + processes_status(ToolsetInstallation, ToolsetUpdate, VMRunnerInstallation))

    machines_list = Gtk.Template.Child()
    host_runner_group = Gtk.Template.Child()
    host_runner_row = Gtk.Template.Child()
    description_label = Gtk.Template.Child()

    def __init__(self, content_navigation_view: Adw.NavigationView, **kwargs):
        super().__init__(**kwargs)
        self.content_navigation_view = content_navigation_view
        host_supported = HOST_RUNNER.supported()
        self.machines_list.set_visible(True)
        self.machines_list.set_sensitive(virtual_machines_supported())
        self.host_runner_group.set_visible(True)
        self.host_runner_group.set_title("Host runner")
        self.host_runner_row.set_sensitive(host_supported)
        self.default_runner_group = Gtk.CheckButton()
        self.host_runner_default_button = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        self.host_runner_default_button.set_group(self.default_runner_group)
        self.host_runner_default_button.set_active(Repository.Settings.value.default_runner_id == "host")
        self.host_runner_default_button.set_tooltip_text("Use Host as the default runner for actions")
        self.host_runner_default_button.connect("toggled", self._on_host_runner_default_toggled)
        self.host_runner_row.add_suffix(self.host_runner_default_button)
        self.host_runner_default_button.set_sensitive(host_supported)
        self.machines_list.show_default_runner = True
        self.machines_list.default_runner_group = self.default_runner_group
        self._update_host_runner_row()
        HOST_RUNNER.event_bus.subscribe(SharedEvent.STATE_UPDATED, self._on_host_runner_updated)
        if virtual_machines_supported():
            self.description_label.set_label(
                "VM runners provide a Linux environment for primary tools and loaded toolsets on macOS. "
                "Create a VM runner before installing a toolset; its builds, updates, and snapshots run there.\n\n"
                + self.description_label.get_label())
        else:
            self.description_label.set_label(
                "The Host runner executes primary tools and loads toolsets directly on this Linux machine. "
                "VM runners are shown for reference but are not available on Linux yet.\n\n" + self.description_label.get_label())

    def _update_host_runner_row(self):
        subtitle = "Runs primary tools and toolsets on this Linux computer"
        if not HOST_RUNNER.supported():
            subtitle = "Unavailable on this platform; macOS uses VM runners"
        elif HOST_RUNNER.users:
            subtitle += " · In use by: " + ", ".join(HOST_RUNNER.users)
        self.host_runner_row.set_subtitle(subtitle)

    def _on_host_runner_updated(self, *_args):
        self._update_host_runner_row()

    def _on_host_runner_default_toggled(self, button):
        if button.get_active():
            Repository.Settings.value.default_runner_id = "host"

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
        self.content_navigation_view.push_view(VMRunnerDetailsView(machine=item, content_navigation_view=self.content_navigation_view), title="VM runner")

    @Gtk.Template.Callback()
    def on_machine_installation_row_pressed(self, sender, installation):
        app_event_bus.emit(AppEvents.PRESENT_VIEW, VMRunnerCreateView(installation_in_progress=installation), "New VM runner", 640, 480)

    @Gtk.Template.Callback()
    def on_add_new_machine_pressed(self, sender):
        app_event_bus.emit(AppEvents.PRESENT_VIEW, VMRunnerCreateView(), "New VM runner", 640, 480)
