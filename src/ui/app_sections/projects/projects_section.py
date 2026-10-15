from gi.repository import Gtk, Adw
from .app_section import app_section
from .project_installation import ProjectInstallation
from .repository import Repository
from .status_indicator import items_status, processes_status
from .project_create_view import ProjectCreateView
from .project_manager import ProjectManager
from .project_update import ProjectUpdate
from .app_events import app_event_bus, AppEvents
from .git_directory_details_view import GitDirectoryDetailsView
from .project_details_view import ProjectDetailsView

@app_section(title="Projects", icon="notes-minimalistic-svgrepo-com-symbolic", order=2_000)
@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/app_sections/projects/projects_section.ui')
class ProjectsSection(Gtk.Box):
    __gtype_name__ = "ProjectsSection"

    @staticmethod
    def section_status():
        """Unsaved changes of projects, projects being created or updated (side menu)."""
        return (items_status(Repository.ProjectDirectory.value) + processes_status(ProjectInstallation, ProjectUpdate))

    def __init__(self, content_navigation_view: Adw.NavigationView, **kwargs):
        super().__init__(**kwargs)
        self.content_navigation_view = content_navigation_view

    @Gtk.Template.Callback()
    def on_item_row_pressed(self, sender, item):
        view = ProjectDetailsView(project_directory=item, content_navigation_view=self.content_navigation_view)
        self.content_navigation_view.push_view(view, title=item.name)

    @Gtk.Template.Callback()
    def on_installation_row_pressed(self, sender, installation):
        app_event_bus.emit(AppEvents.PRESENT_VIEW, ProjectCreateView(installation_in_progress=installation), "New Project", 640, 480)

    @Gtk.Template.Callback()
    def on_add_new_item_pressed(self, sender):
        app_event_bus.emit(AppEvents.PRESENT_VIEW, ProjectCreateView(), "New Project", 640, 480)

