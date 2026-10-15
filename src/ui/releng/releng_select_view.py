from __future__ import annotations
from gi.repository import Gtk, GLib, Gio, GObject, Adw
from .releng_directory import RelengDirectory
from .repository import Repository
from .repository_list_view import ItemRow
from enum import Enum, auto
from .event_bus import EventBus

class RelengSelectionViewEvent(Enum):
    SELECTION_CHANGED = auto() # Means selection was changed

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/releng/releng_select_view.ui')
class RelengSelectionView(Gtk.Box):
    __gtype_name__ = "RelengSelectionView"

    releng_directories_list = Gtk.Template.Child()

    def __init__(self):
        super().__init__()
        self.selected_releng_directory: RelengDirectory | None = None
        self.event_bus = EventBus[RelengSelectionViewEvent]()
        self.connect("realize", self.on_realize)

    def on_realize(self, widget):
        self._fill_rows(Repository.RelengDirectory.value)

    def _fill_rows(self, result: list[RelengDirectory]):
        self.selected_releng_directory = next((releng_directory for releng_directory in result), None)
        self.event_bus.emit(RelengSelectionViewEvent.SELECTION_CHANGED, self)
        if not result:
            error_label = Gtk.Label(label="There are no Releng directories yet. Create one in the Releng section.")
            error_label.set_wrap(True)
            error_label.set_halign(Gtk.Align.CENTER)
            error_label.set_margin_top(12)
            error_label.set_margin_bottom(12)
            error_label.set_margin_start(24)
            error_label.set_margin_end(24)
            error_label.add_css_class("dimmed")
            self.releng_directories_list.add(error_label)
        releng_directories_check_buttons_group = []
        for releng_directory in result:
            row = ItemRow(
                item=releng_directory,
                item_title_property_name="name",
                item_subtitle_property_name="short_details",
                item_status_property_name="status_indicator_values",
                item_icon="book-minimalistic-svgrepo-com-symbolic"
            )
            check_button = Gtk.CheckButton()
            check_button.set_active(releng_directory == self.selected_releng_directory)
            if releng_directories_check_buttons_group:
                check_button.set_group(releng_directories_check_buttons_group[0])
            check_button.connect("toggled", self._on_releng_directory_selected, releng_directory)
            releng_directories_check_buttons_group.append(check_button)
            row.add_prefix(check_button)
            row.set_activatable_widget(check_button)
            row.set_sensitive(True)
            self.releng_directories_list.add(row)

    def _on_releng_directory_selected(self, button: Gtk.CheckButton, releng_directory: RelengDirectory):
        """Callback for when a row's checkbox is toggled."""
        if button.get_active():
            self.selected_releng_directory = releng_directory
        else:
            # Deselect if unchecked
            if self.selected_releng_directory == releng_directory:
                self.selected_releng_directory = None
        self.event_bus.emit(RelengSelectionViewEvent.SELECTION_CHANGED, self)

