import weakref
from gi.repository import Gtk, GLib
from .app_events import AppEvents, app_event_bus
from .app_section import AppSection
from .main_window_side_menu_button import MainWindowSideMenuButton
from .main_window_processes_list import ProcessesList
from .repository import Repository, RepositoryEvent
from .multistage_process import MultiStageProcess, MultiStageProcessEvent
from .event_bus import SharedEvent

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/main_window/main_window_side_menu.ui')
class CatalystlabWindowSideMenu(Gtk.Box):
    __gtype_name__ = 'CatalystlabWindowSideMenu'

    # View elements:
    section_list = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Load main sections and add buttons for them.
        for section in AppSection.all_sections:
            if section.section_details.show_in_side_bar:
                button = MainWindowSideMenuButton(section)
                self.section_list.append(button)
        app_event_bus.subscribe(AppEvents.OPEN_APP_SECTION, self.opened_app_section)
        # Running and failed installers, at the bottom.
        # Kept here: event bus refers to it weakly, its Python object would be collected otherwise.
        self.processes_list = ProcessesList()
        self.append(self.processes_list)
        # Statuses of sections come from items with status indicators (toolsets, machines, git directories) and
        # running processes. Their state events refresh side menu, like they refresh rows in sections.
        # Observed items and processes, weak so removed ones are forgotten (their ids can be reused by new objects).
        self._observed = weakref.WeakSet()
        self._statuses_update_scheduled = False
        for repository in self._status_repositories():
            repository.event_bus.subscribe(RepositoryEvent.VALUE_CHANGED, self._on_items_changed)
        MultiStageProcess.event_bus.subscribe(MultiStageProcessEvent.STARTED_PROCESSES_CHANGED, self._on_items_changed)
        self._on_items_changed()
        # Set initial selected page
        self.selected_section: AppSection = None

    @staticmethod
    def _status_repositories() -> list:
        return [Repository.ProjectDirectory, Repository.Toolset, Repository.Runner, Repository.RelengDirectory,
                Repository.OverlayDirectory]

    def _on_items_changed(self, *args):
        """Items or processes were added or removed (repositories send VALUE_CHANGED when their lists change, processes
        STARTED_PROCESSES_CHANGED when started or cleared): observe new ones and refresh."""
        for repository in self._status_repositories():
            for item in repository.value:
                self._observe(getattr(item, "event_bus", None), SharedEvent.STATE_UPDATED, item)
        for process in MultiStageProcess.started_processes:
            self._observe(process.event_bus, MultiStageProcessEvent.STATE_CHANGED, process)
        self._schedule_statuses_update()

    def _observe(self, event_bus, event, owner):
        if event_bus is None or owner in self._observed:
            return
        self._observed.add(owner)
        event_bus.subscribe(event, self._schedule_statuses_update)

    def _schedule_statuses_update(self, *args):
        # Several events can come at once, statuses are updated once.
        if self._statuses_update_scheduled:
            return
        self._statuses_update_scheduled = True
        GLib.idle_add(self._update_statuses)

    def _update_statuses(self):
        self._statuses_update_scheduled = False
        row = self.section_list.get_first_child()
        while row:
            if isinstance(row, MainWindowSideMenuButton):
                row.update_status()
            row = row.get_next_sibling()
        return False

    def opened_app_section(self, section: AppSection):
        # States shown in side menu are seen when their section is opened.
        selected_row = None
        row = self.section_list.get_first_child()
        while row:
            if isinstance(row, MainWindowSideMenuButton):
                row.set_section_open(row.section == section)
                if row.section == section:
                    selected_row = row
            row = row.get_next_sibling()
        if self.selected_section == section:
            return
        self.selected_section = section
        self.section_list.select_row(selected_row)

    # Callback received when the selected row changes.
    @Gtk.Template.Callback()
    def row_selected(self, _, row):
        if row and hasattr(row, "section"):
            if row.section != self.selected_section:
                self.selected_section = row.section
                app_event_bus.emit(AppEvents.OPEN_APP_SECTION, row.section)

