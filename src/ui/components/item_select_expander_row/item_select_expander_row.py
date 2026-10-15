from __future__ import annotations
from gi.repository import Gtk, GLib, Gio, GObject, Adw
from enum import Enum, auto
from .repository import Repository, RepositoryEvent
from .repository_list_view import ItemRow
from .event_bus import EventBus
from .scroll_position import preserved_scroll_position
from .item_select_view import ItemSelectionViewEvent

class ItemSelectionExpanderRow(Adw.ExpanderRow):
    __gtype_name__ = "ItemSelectionExpanderRow"

    __gsignals__ = {
        "is-item-selectable": (GObject.SignalFlags.RUN_FIRST, bool, (GObject.TYPE_PYOBJECT,)),
        "is-item-usable": (GObject.SignalFlags.RUN_FIRST, bool, (GObject.TYPE_PYOBJECT,)),
        "setup-items-monitoring": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,))
    }

    # Properties:
    title = GObject.Property(type=str, default=None)
    item_class_name = GObject.Property(type=str, default=None)
    item_icon = GObject.Property(type=str, default=None)
    item_title_property_name = GObject.Property(type=str, default=None)
    item_subtitle_property_name = GObject.Property(type=str, default=None)
    item_status_property_name = GObject.Property(type=str, default=None)
    item_unsupported_property_name = GObject.Property(type=str, default=None)
    autoselect_default = GObject.Property(type=bool, default=False)
    display_none = GObject.Property(type=bool, default=False)
    none_title = GObject.Property(type=str, default="None")
    none_subtitle = GObject.Property(type=str, default=None)
    allow_multiselect = GObject.Property(type=bool, default=False)

    def __init__(self):
        super().__init__()
        self.selected_item = None
        self.selected_items = None
        self.event_bus = EventBus[ItemSelectionViewEvent]()
        self._load_labels()
        self._add_warning_icon()
        self.connect("realize", self.on_realize)

    def set_static_list(self, list: list):
        """Use static list insetad of repository. Only call this once."""
        self.static_list = list
        self._load_items()

    def on_realize(self, widget):
        self.set_title(self.title if self.title else "")
        if self.item_class_name:
            self.item_class = globals().get(self.item_class_name)
            self.repository = getattr(Repository, self.item_class_name)
            if hasattr(self, 'static_list'):
                raise ValueError("Canno't use both static_list and item_class_name")
            self._load_items()
            # Items added or removed while view is displayed (eg. folder created in other view).
            if not getattr(self, "_repository_observed", False):
                self._repository_observed = True
                self.repository.event_bus.subscribe(RepositoryEvent.VALUE_CHANGED, self._on_repository_changed)

    def _add_warning_icon(self):
        self.warning_icon = Gtk.Image.new_from_icon_name("danger-triangle-svgrepo-com-symbolic")
        self.warning_icon.add_css_class("warning")
        self.warning_icon.set_visible(False) # Shown by determine_incorrect_selection once items are loaded.
        self.add_suffix(self.warning_icon)

    def _on_repository_changed(self, *args):
        with preserved_scroll_position(self):
            self._load_items()

    def set_leading_items(self, items: list):
        """Items shown before items of repository (or static list), eg. option that isn't stored in repository."""
        self.leading_items = items
        if hasattr(self, 'rows'):
            self._load_items()

    def items(self) -> list:
        items = self.static_list if hasattr(self, 'static_list') else self.repository.value if hasattr(self, 'repository') else []
        return getattr(self, 'leading_items', []) + list(items)

    def select(self, item):
        self.selected_item = item
        self.event_bus.emit(ItemSelectionViewEvent.ITEM_CHANGED, self)
        self.display_selected_item()
        self.determine_incorrect_selection()

    def multiselect(self, items):
        self.selected_items = items
        self.event_bus.emit(ItemSelectionViewEvent.ITEM_CHANGED, self)
        self.display_selected_item()
        self.determine_incorrect_selection()

    def refresh_items_state(self, data):
        """Call this from class using this view when monitoring_usable_changes triggers an update"""
        self.is_not_usable_label.set_visible(self.selected_item and not self.emit("is-item-usable", self.selected_item))
        valid_items = [item for item in self.items() if self.emit("is-item-selectable", item)]
        self.no_valid_entries_label.set_visible(not valid_items)
        for row in getattr(self, 'rows', []):
            row.set_sensitive(row.item in valid_items)
        self.event_bus.emit(ItemSelectionViewEvent.ITEM_CHANGED, self)

    def _load_labels(self):
        # TODO: Propertly handling adding/removind/hiding these labels when needed
        # No items label
        self.no_valid_entries_label = Gtk.Label(label=f"No valid items available")
        self.no_valid_entries_label.set_wrap(True)
        self.no_valid_entries_label.set_halign(Gtk.Align.CENTER)
        self.no_valid_entries_label.set_margin_top(12)
        self.no_valid_entries_label.set_margin_bottom(12)
        self.no_valid_entries_label.set_margin_start(24)
        self.no_valid_entries_label.set_margin_end(24)
        self.no_valid_entries_label.add_css_class("dimmed")
        #self.add_row(self.no_valid_entries_label)
        # Is not usable label
        self.is_not_usable_label = Gtk.Label(label="This item is currently in use.")
        self.is_not_usable_label.set_wrap(True)
        self.is_not_usable_label.set_halign(Gtk.Align.CENTER)
        self.is_not_usable_label.set_margin_top(12)
        self.is_not_usable_label.set_margin_bottom(12)
        self.is_not_usable_label.set_margin_start(24)
        self.is_not_usable_label.set_margin_end(24)
        self.is_not_usable_label.add_css_class("dimmed")
        #self.add_row(self.is_not_usable_label)

    def _load_items(self):
        valid_items = [item for item in self.items() if self.emit("is-item-selectable", item)]
        # Monitor valid items for is_reserved changes
        self.emit("setup-items-monitoring", self.items())
        # Select initial item
        if self.autoselect_default:
            self.selected_item = next(
                (item for item in valid_items if self.emit("is-item-usable", item)),
                valid_items[0] if valid_items else None
            )
            self.event_bus.emit(ItemSelectionViewEvent.ITEM_CHANGED, self)
            self.display_selected_item()
        # Create rows
        items_check_buttons_group = []

        hidden_check_box = Gtk.CheckButton()
        items_check_buttons_group.append(hidden_check_box)

        if hasattr(self, 'rows'):
            for row in self.rows:
                self.remove(row)
        if hasattr(self, 'none_row') and self.none_row:
            self.remove(self.none_row)
            del self.none_row

        if self.display_none and not hasattr(self, 'none_row') and not self.allow_multiselect:
            self.none_row = Adw.ActionRow(title=self.none_title, subtitle=self.none_subtitle)
            check_button = Gtk.CheckButton()
            check_button.set_active(self.selected_item == None)
            check_button.connect("toggled", self._on_item_selected, None)
            if items_check_buttons_group and self.allow_multiselect == False:
                check_button.set_group(items_check_buttons_group[0])
            items_check_buttons_group.append(check_button)
            self.none_row.add_prefix(check_button)
            self.none_row.set_activatable_widget(check_button)
            self.add_row(self.none_row)

        self.rows = []
        for item in self.items():
            row = ItemRow(
                item=item,
                item_title_property_name=self.item_title_property_name,
                item_subtitle_property_name=self.item_subtitle_property_name,
                item_status_property_name=self.item_status_property_name,
                item_unsupported_property_name=self.item_unsupported_property_name,
                item_icon=self.item_icon,
                item_icon_resolver=getattr(self, "item_icon_resolver", None)
            )
            check_button = Gtk.CheckButton()
            if self.allow_multiselect:
                check_button.set_active(item in self.selected_items if self.selected_items else False)
            else:
                check_button.set_active(item == self.selected_item)
            if items_check_buttons_group and self.allow_multiselect == False:
                check_button.set_group(items_check_buttons_group[0])
            check_button.connect("toggled", self._on_item_selected, item)
            items_check_buttons_group.append(check_button)
            row.add_prefix(check_button)
            row.set_activatable_widget(check_button)
            row.set_sensitive(item in valid_items)
            self.add_row(row)
            self.rows.append(row)
        # Footer rows (eg. button creating new item) stay below items.
        for footer_row in getattr(self, "_footer_rows", []):
            self.remove(footer_row)
            self.add_row(footer_row)
        self.no_valid_entries_label.set_visible(not valid_items)
        self.is_not_usable_label.set_visible(self.selected_item and not self.emit("is-item-usable", self.selected_item))
        self.determine_incorrect_selection()
        self.display_selected_item()

    def _on_item_selected(self, button: Gtk.CheckButton, item: item):
        """Callback for when a row's checkbox is toggled."""
        if button.get_active():
            if self.allow_multiselect:
                self.selected_items.append(item)
            else:
                self.selected_item = item
        else:
            if self.allow_multiselect:
                self.selected_items.remove(item)
            if len(self.rows) == 1 and not self.display_none or len(self.rows) == 0 and self.display_none:
                button.set_active(True)
        self.display_selected_item()
        # Schedule a single emission in the idle loop
        if not hasattr(self, '_emit_idle_id'):
            self._emit_idle_id = None
        if self._emit_idle_id is None:
            self._emit_idle_id = GLib.idle_add(self._emit_selection_change)
        self.determine_incorrect_selection()

    def _emit_selection_change(self):
        self._emit_idle_id = None
        self.event_bus.emit(ItemSelectionViewEvent.ITEM_CHANGED, self)
        return False

    def display_selected_item(self):
        if self.allow_multiselect:
            self.set_subtitle(", ".join(item.display for item in self.selected_items) if self.selected_items else f"({self.none_title})")
        else:
            self.set_subtitle(getattr(self.selected_item, self.item_title_property_name, self.selected_item if isinstance(self.selected_item, str) else "(Selected)") if self.selected_item else f"({self.none_title})")

    def add_footer_row(self, row: Gtk.Widget):
        """Row shown below items when expanded, kept there when items are loaded again."""
        if not hasattr(self, "_footer_rows"):
            self._footer_rows = []
        self._footer_rows.append(row)
        self.add_row(row)

    def set_extra_warning(self, text: str | None):
        """Warning about selected item that depends on context (eg. folder made for other architecture than project),
        shown with warning icon and its tooltip."""
        self._extra_warning = text
        self.determine_incorrect_selection()

    def determine_incorrect_selection(self):
        """If not available item is set as selected display warning."""
        if hasattr(self, 'repository') or hasattr(self, 'static_list'):
            if self.allow_multiselect:
                items_empty = not self.selected_items
                contains_unsupported_items = any(
                    getattr(item, self.item_unsupported_property_name, False)
                    for item in self.selected_items
                    if item and self.item_unsupported_property_name
                )
                self.warning_icon.set_visible((items_empty and not self.display_none) or contains_unsupported_items)
            else:
                item_not_found = self.selected_item not in self.items()
                item_none = self.selected_item is None
                item_unsupported = getattr(self.selected_item, self.item_unsupported_property_name, False) if self.selected_item and self.item_unsupported_property_name else False
                self.warning_icon.set_visible(
                    (item_not_found and not(item_none and self.display_none))
                    or item_unsupported
                    or bool(getattr(self, "_extra_warning", None))
                )
                self.warning_icon.set_tooltip_text(getattr(self, "_extra_warning", None))

