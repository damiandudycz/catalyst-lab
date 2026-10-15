from __future__ import annotations
import os
from gi.repository import Gtk, Adw
from .build_machine_installation import BuildMachineInstallation
from .build_machine_manager import BuildMachineManager
from .multistage_process import MultiStageProcessState
from .wizard_view import WizardView

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/build_machine/build_machine_create_view.ui')
class BuildMachineCreateView(Gtk.Box):
    __gtype_name__ = "BuildMachineCreateView"

    wizard_view = Gtk.Template.Child()
    configuration_page = Gtk.Template.Child()
    name_row = Gtk.Template.Child()
    name_used_label = Gtk.Template.Child()
    cpus_row = Gtk.Template.Child()
    memory_row = Gtk.Template.Child()
    disk_row = Gtk.Template.Child()
    swap_row = Gtk.Template.Child()

    def __init__(self, installation_in_progress: BuildMachineInstallation | None = None, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__()
        self.installation_in_progress = installation_in_progress
        self.content_navigation_view = content_navigation_view
        self.name_row.set_text(self._default_name())
        # Half of CPUs of this computer by default.
        self.cpus_row.set_value(max(1, min(8, (os.cpu_count() or 4) // 2)))
        self.connect("realize", self.on_realize)

    def on_realize(self, widget):
        self.wizard_view.content_navigation_view = self.content_navigation_view
        self.wizard_view._window = self._window
        self.wizard_view.set_installation(self.installation_in_progress)

    def _default_name(self) -> str:
        index = 1
        while not BuildMachineManager.shared().is_name_available("Build machine" if index == 1 else f"Build machine {index}"):
            index += 1
        return "Build machine" if index == 1 else f"Build machine {index}"

    @Gtk.Template.Callback()
    def on_name_changed(self, row):
        self.name_used_label.set_visible(bool(row.get_text().strip()) and not BuildMachineManager.shared().is_name_available(row.get_text()))
        self.wizard_view._refresh_buttons_state()

    @Gtk.Template.Callback()
    def is_page_ready_to_continue(self, sender, page) -> bool:
        if page == self.configuration_page:
            return BuildMachineManager.shared().is_name_available(self.name_row.get_text())
        return True

    @Gtk.Template.Callback()
    def begin_installation(self, view):
        installation_in_progress = BuildMachineInstallation(
            name=self.name_row.get_text().strip(),
            cpus=int(self.cpus_row.get_value()),
            memory_gib=int(self.memory_row.get_value()),
            workspace_gib=int(self.disk_row.get_value()),
            swap_gib=int(self.swap_row.get_value()),
        )
        installation_in_progress.start()
        self.wizard_view.set_installation(installation_in_progress)
