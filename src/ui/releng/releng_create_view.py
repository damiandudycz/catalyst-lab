from __future__ import annotations
from gi.repository import Gtk, Adw
from .multistage_process import MultiStageProcessState
from .releng_installation import RelengInstallation
from .git_directory_create_config_view import GitDirectoryCreateConfigViewEvent, GitDirectorySetupConfiguration, GitDirectorySource
from .wizard_view import WizardView

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/releng/releng_create_view.ui')
class RelengCreateView(Gtk.Box):
    __gtype_name__ = "RelengCreateView"

    # Main views:
    wizard_view = Gtk.Template.Child()
    # Setup view elements:
    config_page = Gtk.Template.Child()
    config_view = Gtk.Template.Child()

    def __init__(self, installation_in_progress: RelengInstallation | None = None, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__()
        self.installation_in_progress = installation_in_progress
        self.content_navigation_view = content_navigation_view
        self.config_view.event_bus.subscribe(
            GitDirectoryCreateConfigViewEvent.CONFIGURATION_READY_CHANGED,
            self.config_ready_changed
        )
        self.connect("realize", self.on_realize)

    def config_ready_changed(self, data):
        self.wizard_view._refresh_buttons_state()

    def on_realize(self, widget):
        self.wizard_view.content_navigation_view = self.content_navigation_view
        self.wizard_view._window = self._window
        self.wizard_view.set_installation(self.installation_in_progress)

    @Gtk.Template.Callback()
    def is_page_ready_to_continue(self, sender, page) -> bool:
        match page:
            case self.config_page:
                return self.config_view.configuration_ready
        return True

    @Gtk.Template.Callback()
    def begin_installation(self, view):
        self._start_installation(configuration=self.config_view.get_configuration())

    def _start_installation(self, configuration: GitDirectorySetupConfiguration):
        if configuration.source != GitDirectorySource.GIT_REPOSITORY:
            raise ValueError("Releng installation accepts only GitDirectorySource.GIT_REPOSITORY as source")
        installation_in_progress = RelengInstallation(configuration=configuration)
        installation_in_progress.start()
        self.wizard_view.set_installation(installation_in_progress)

