from __future__ import annotations
from gi.repository import Gtk, Adw
from .multistage_process import MultiStageProcessState
from .overlay_installation import OverlayInstallation
from .git_directory_create_config_view import GitDirectoryCreateConfigViewEvent
from .git_directory_default_content_builder import DefaultDirContentBuilder
from .git_installation import GitDirectorySetupConfiguration
from .wizard_view import WizardView
import os

class DefaultOverlayDirContentBuilder(DefaultDirContentBuilder):
    def build_in(self, path: str, repo_name: str):
        """Create default files in given path for new overlay.
        Args:
            path (str): Base directory where the overlay structure will be created.
            repo_name (str): Name of the overlay (used in metadata/layout.conf and profiles/repo_name).
        """
        structure = ['metadata', 'profiles']
        for folder in structure:
            os.makedirs(os.path.join(path, folder), exist_ok=True)
        # Create metadata/layout.conf:
        layout_conf_path = os.path.join(path, 'metadata', 'layout.conf')
        with open(layout_conf_path, 'w') as f:
            f.write(f"repo-name = {repo_name}\n")
        # Create profiles/repo_name:
        repo_name_path = os.path.join(path, 'profiles', 'repo_name')
        with open(repo_name_path, 'w') as f:
            f.write(f"{repo_name}\n")

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/overlay/overlay_create_view.ui')
class OverlayCreateView(Gtk.Box):
    __gtype_name__ = "OverlayCreateView"

    # Main views:
    wizard_view = Gtk.Template.Child()
    # Setup view elements:
    config_page = Gtk.Template.Child()
    config_view = Gtk.Template.Child()

    def __init__(self, installation_in_progress: OverlayInstallation | None = None, content_navigation_view: Adw.NavigationView | None = None):
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
        self._start_installation(configuration=self.config_view.get_configuration(default_dir_content_builder=DefaultOverlayDirContentBuilder()))

    def _start_installation(self, configuration: GitDirectorySetupConfiguration):
        installation_in_progress = OverlayInstallation(configuration=configuration)
        installation_in_progress.start()
        self.wizard_view.set_installation(installation_in_progress)

