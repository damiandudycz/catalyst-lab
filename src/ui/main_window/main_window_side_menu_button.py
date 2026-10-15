from gi.repository import Gtk
from .app_section import AppSection
from .status_indicator import StatusIndicator, StatusIndicatorValues, combined_status, unseen_status_entries

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/main_window/main_window_side_menu_button.ui')
class MainWindowSideMenuButton(Gtk.ListBoxRow):
    __gtype_name__ = "MainWindowSideMenuButton"

    # Template children
    label = Gtk.Template.Child()
    icon = Gtk.Template.Child()

    def __init__(self, section: AppSection):
        super().__init__()
        self.section = section
        self.set_tooltip_text(section.section_details.label)
        self.label.set_label(section.section_details.label)
        self.icon.set_from_icon_name(section.section_details.icon)
        self.status_indicator = StatusIndicator()
        self.status_indicator.set_valign(Gtk.Align.CENTER)
        self.status_indicator.set_visible(False)
        self.label.get_parent().append(self.status_indicator)
        # States of items seen in section, see unseen_status_entries.
        self._seen: set = set()
        self._section_open = False

    def set_section_open(self, section_open: bool):
        """Section is displayed, its states are seen."""
        self._section_open = section_open
        self.update_status()

    def update_status(self):
        """Shows status of section (running operations, unsaved changes...), like status of its items."""
        status_function = getattr(self.section, "section_status", None)
        try:
            entries = status_function() if status_function else []
        except Exception as e:
            print(f"Failed to get status of section {self.section.section_details.title}: {e}")
            entries = []
        unseen, self._seen = unseen_status_entries(entries, self._seen, self._section_open)
        values: StatusIndicatorValues | None = combined_status(unseen)
        self.status_indicator.set_visible(values is not None)
        if values is not None and values != getattr(self, "_status_values", None):
            self.status_indicator.set_values(values)
        self._status_values = values
