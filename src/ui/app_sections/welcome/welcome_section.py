from gi.repository import Gtk, Adw
from .app_events import AppEvents, app_event_bus
from .app_section import AppSection, app_section
from .repository import Repository, RepositoryEvent, DEFAULT_LIBRARY_DIRECTORY, library_directory
from .initial_setup_page import InitialSetupPage
from .library import is_library
import os

@app_section(title="Welcome", label="Home", icon="go-home-symbolic", show_in_side_bar=False, show_side_bar=False, order=1_000)
@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/app_sections/welcome/welcome_section.ui')
class WelcomeSection(Gtk.Box):
    __gtype_name__ = "WelcomeSection"

    default_actions_section = Gtk.Template.Child()
    first_run_section = Gtk.Template.Child()

    def __init__(self, content_navigation_view: Adw.NavigationView, **kwargs):
        super().__init__(**kwargs)
        self.content_navigation_view = content_navigation_view
        self.setup_sections_visibility()
        Repository.Settings.event_bus.subscribe(
            RepositoryEvent.VALUE_CHANGED,
            self.setup_sections_visibility
        )

    def setup_sections_visibility(self, _ = None):
        settings = Repository.Settings.value
        default_library = os.path.abspath(os.path.expanduser(DEFAULT_LIBRARY_DIRECTORY))
        if (not settings.initial_setup_done and
                os.path.realpath(library_directory()) == os.path.realpath(default_library) and
                is_library(default_library)):
            settings.initial_setup_done = True
        initial_setup_done = settings.initial_setup_done
        self.default_actions_section.set_visible(initial_setup_done)
        self.first_run_section.set_visible(not initial_setup_done)

    # --------------------------------------------------------------------------
    # Navigation:

    @Gtk.Template.Callback()
    def on_start_row_activated(self, _):
        # Setup is done when its last step is finished.
        InitialSetupPage.push(self.content_navigation_view)

    @Gtk.Template.Callback()
    def on_projects_row_activated(self, _):
        app_event_bus.emit(
            AppEvents.OPEN_APP_SECTION,
            AppSection.ProjectsSection
        )

    @Gtk.Template.Callback()
    def on_builds_row_activated(self, _):
        app_event_bus.emit(
            AppEvents.OPEN_APP_SECTION,
            AppSection.BuildsSection
        )

    @Gtk.Template.Callback()
    def on_deploy_row_activated(self, _):
        app_event_bus.emit(
            AppEvents.OPEN_APP_SECTION,
            AppSection.DeploySection
        )
