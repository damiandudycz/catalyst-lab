from __future__ import annotations
from gi.repository import Gtk, GLib, GObject, Adw
from enum import Enum, auto
from .repository import Repository, RepositoryEvent
from .repository_list_view import ItemRow
from .event_bus import EventBus
from .scroll_position import preserved_scroll_position
from .item_select_state import ItemSelectionData, no_valid_items_visible

class ItemSelectionViewEvent(Enum):
    ITEM_CHANGED = auto() # Means selection was changed or currently selected state changed.

def update_selection_messages(view, valid_items=None, allow_multiselect=False):
    items = view.items()
    if valid_items is None:
        selectable_items = getattr(view, "selectable_items", None)
        if callable(selectable_items):
            valid_items = selectable_items()
        else:
            # Expander rows share this helper but expose selectability as a
            # signal rather than the list-view's selectable_items() method.
            valid_items = [item for item in items if view.emit("is-item-selectable", item)]
    selected_items = (view.selected_items or []) if allow_multiselect else [view.selected_item]
    has_supported_selection = any(
        item in items and not getattr(item, "unsupported", False)
        for item in selected_items if item is not None
    )
    view.no_valid_entries_label.set_visible(
        no_valid_items_visible(valid_items, view.display_none, allow_multiselect, has_supported_selection)
    )
    selected = view.selected_item
    unusable = not allow_multiselect and selected is not None and not view.emit("is-item-usable", selected)
    view.is_not_usable_label.set_visible(bool(unusable))


def schedule_selection_change(view):
    if getattr(view, "_emit_idle_id", None) is None:
        view._emit_idle_id = GLib.idle_add(_emit_selection_change, view)


def _emit_selection_change(view):
    view._emit_idle_id = None
    view.event_bus.emit(ItemSelectionViewEvent.ITEM_CHANGED, view)
    return False


def _selection_message(text: str) -> Gtk.Label:
    return Gtk.Label(label=text, wrap=True, halign=Gtk.Align.CENTER, margin_top=12, margin_bottom=12,
                     margin_start=24, margin_end=24, css_classes=["dimmed"])


def _selection_item_row(view, item, valid_items, button_group, allow_multiselect=False):
    row = ItemRow(
        item=item,
        item_title_property_name=view.item_title_property_name,
        item_subtitle_property_name=view.item_subtitle_property_name,
        item_status_property_name=view.item_status_property_name,
        item_unsupported_property_name=view.item_unsupported_property_name,
        item_icon=view.item_icon,
        item_icon_resolver=getattr(view, "item_icon_resolver", None),
    )
    button = Gtk.CheckButton()
    selected = item in (view.selected_items or []) if allow_multiselect else item == view.selected_item
    button.set_active(selected)
    if not allow_multiselect:
        button.set_group(button_group)
    button.connect("toggled", view._on_item_selected, item)
    row.add_prefix(button)
    row.set_activatable_widget(button)
    row.set_sensitive(item in valid_items)
    return row


@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/components/item_select/item_select_view.ui')
class ItemSelectionView(ItemSelectionData, Gtk.Box):
    __gtype_name__ = "ItemSelectionView"

    __gsignals__ = {
        "is-item-selectable": (GObject.SignalFlags.RUN_FIRST, bool, (GObject.TYPE_PYOBJECT,)),
        "is-item-usable": (GObject.SignalFlags.RUN_FIRST, bool, (GObject.TYPE_PYOBJECT,)),
        "setup-items-monitoring": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
    }

    def do_is_item_usable(self, item) -> bool:
        """Items are usable unless a view marks them unavailable, such as a reserved toolset."""
        return True

    def selectable_items(self):
        items = self.items()
        if self.include_host_runner:
            from .runner import runner_is_supported
            return [item for item in items if runner_is_supported(item) and self.emit("is-item-selectable", item)]
        return [item for item in items if self.emit("is-item-selectable", item)]

    # View elements:
    items_list = Gtk.Template.Child()
    # Properties:
    title = GObject.Property(type=str, default=None)
    description = GObject.Property(type=str, default=None)
    item_class_name = GObject.Property(type=str, default=None)
    item_icon = GObject.Property(type=str, default=None)
    item_title_property_name = GObject.Property(type=str, default=None)
    item_subtitle_property_name = GObject.Property(type=str, default=None)
    item_status_property_name = GObject.Property(type=str, default=None)
    item_unsupported_property_name = GObject.Property(type=str, default=None)
    include_host_runner = GObject.Property(type=bool, default=False)
    autoselect_default = GObject.Property(type=bool, default=False)
    display_none = GObject.Property(type=bool, default=False)
    none_title = GObject.Property(type=str, default="None")
    none_subtitle = GObject.Property(type=str, default=None)

    def __init__(self):
        super().__init__()
        self.selected_item = None
        self.event_bus = EventBus[ItemSelectionViewEvent]()
        self._load_labels()
        self.connect("realize", self.on_realize)

    def on_realize(self, widget):
        self.items_list.set_title(GLib.markup_escape_text(self.title) if self.title else "")
        self.items_list.set_description(self.description or None)
        if self.item_class_name:
            self.repository = getattr(Repository, self.item_class_name)
            if hasattr(self, 'static_list'):
                raise ValueError("Canno't use both static_list and item_class_name")
            if self.include_host_runner:
                from .runner import HOST_RUNNER
                self.set_leading_items([HOST_RUNNER])
            self._load_items()
            # Items added or removed while view is displayed (eg. folder created in other view).
            if not getattr(self, "_repository_observed", False):
                self._repository_observed = True
                self.repository.event_bus.subscribe(RepositoryEvent.VALUE_CHANGED, self._on_repository_changed)

    def _on_repository_changed(self, *args):
        with preserved_scroll_position(self):
            self._load_items()

    def select(self, item):
        self.selected_item = item
        update_selection_messages(self)
        self.emit("selection-changed", self)
        self.event_bus.emit(ItemSelectionViewEvent.ITEM_CHANGED, self)

    def refresh_items_state(self, *_):
        """Call this from class using this view when monitoring_usable_changes triggers an update"""
        valid_items = self.selectable_items()
        for row in self.rows:
            row.set_sensitive(row.item in valid_items)
        update_selection_messages(self, valid_items)
        self.emit("selection-changed", self)
        self.event_bus.emit(ItemSelectionViewEvent.ITEM_CHANGED, self)

    def _load_labels(self):
        self.no_valid_entries_label = _selection_message("No valid items available")
        self.items_list.add(self.no_valid_entries_label)
        self.is_not_usable_label = _selection_message("This item is currently in use.")
        self.items_list.add(self.is_not_usable_label)

    def _load_items(self):
        valid_items = self.selectable_items()
        # Monitor valid items for is_reserved changes
        self.emit("setup-items-monitoring", self.items())
        # Select initial item
        if self.autoselect_default:
            preferred = getattr(self, "preferred_item", ...)
            if preferred is not ... and (preferred in valid_items or preferred is None and self.display_none):
                self.selected_item = preferred
            else:
                self.selected_item = next(
                    (item for item in valid_items if self.emit("is-item-usable", item)),
                    valid_items[0] if valid_items else None
                )
            self.emit("selection-changed", self)
            self.event_bus.emit(ItemSelectionViewEvent.ITEM_CHANGED, self)
        # Create rows
        check_button_group = Gtk.CheckButton()

        if hasattr(self, 'rows'):
            for row in self.rows:
                self.items_list.remove(row)
        if hasattr(self, 'none_row') and self.none_row:
            self.items_list.remove(self.none_row)
            del self.none_row

        if self.display_none and not hasattr(self, 'none_row'):
            self.none_row = Adw.ActionRow(title=self.none_title, subtitle=self.none_subtitle)
            check_button = Gtk.CheckButton()
            check_button.set_active(self.selected_item == None)
            check_button.connect("toggled", self._on_item_selected, None)
            check_button.set_group(check_button_group)
            self.none_row.add_prefix(check_button)
            self.none_row.set_activatable_widget(check_button)
            self.items_list.add(self.none_row)

        self.rows = []
        for item in self.items():
            row = _selection_item_row(self, item, valid_items, check_button_group)
            self.items_list.add(row)
            self.rows.append(row)
        update_selection_messages(self, valid_items)

    def _on_item_selected(self, button: Gtk.CheckButton, item: item):
        """Callback for when a row's checkbox is toggled."""
        if button.get_active():
            self.selected_item = item
            update_selection_messages(self)
            self.emit("selection-changed", self)
        else:
            if len(self.rows) == 1 and not self.display_none or len(self.rows) == 0 and self.display_none:
                button.set_active(True)
        schedule_selection_change(self)

