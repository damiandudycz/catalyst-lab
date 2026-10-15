from gi.repository import Gtk, Adw

# ------------------------------------------------------------------------------
# Popups of combo rows show whole items. Adwaita shortens long items (eg. package names) with ellipsis, so items
# differing at the end look the same. Items are shown in full, popup grows to fit them, selected one has check mark
# like in default popup. Rows themselves still shorten selected value when there's no space.

def fit_popup_to_items(row: Adw.ComboRow):
    """Uses popup items showing whole strings (rows with Gtk.StringList models)."""
    handlers: dict = {} # List item -> handler of row selection.
    factory = Gtk.SignalListItemFactory()

    def setup(factory, list_item):
        box = Gtk.Box(spacing=6)
        label = Gtk.Label(xalign=0, hexpand=True)
        check = Gtk.Image(icon_name="object-select-symbolic", opacity=0)
        box.append(label)
        box.append(check)
        list_item.set_child(box)

    def bind(factory, list_item):
        label = list_item.get_child().get_first_child()
        check = label.get_next_sibling()
        item = list_item.get_item()
        label.set_label(item.get_string() if hasattr(item, "get_string") else str(item))
        def update(*args):
            check.set_opacity(1 if row.get_selected_item() == item else 0)
        update()
        handlers[list_item] = row.connect("notify::selected-item", update)

    def unbind(factory, list_item):
        if (handler := handlers.pop(list_item, None)) is not None:
            row.disconnect(handler)

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    factory.connect("unbind", unbind)
    row.set_list_factory(factory)
    row._fit_popup_factory = factory # Kept with row.
    return row
