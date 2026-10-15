from contextlib import contextmanager
from gi.repository import Gtk, GLib

@contextmanager
def preserved_scroll_position(widget: Gtk.Widget):
    """Keeps scroll position of scrolled window containing widget while its content is rebuilt (rows removed and
    created again). Focus is cleared first, otherwise focus moving from removed row scrolls view to other widget.
    Position is restored when size of new content is known."""
    scrolled_window = widget.get_ancestor(Gtk.ScrolledWindow)
    if scrolled_window is None:
        yield
        return
    adjustment = scrolled_window.get_vadjustment()
    value = adjustment.get_value()
    root = widget.get_root()
    focus = root.get_focus() if root else None
    if focus is not None and (focus == widget or focus.is_ancestor(widget)):
        root.set_focus(None)
    try:
        yield
    finally:
        def restore(*args):
            adjustment.set_value(min(value, max(adjustment.get_upper() - adjustment.get_page_size(), 0)))
        restore()
        # Size of rebuilt content changes after layout, position is set again then (once).
        handler = None
        def on_changed(adjustment):
            nonlocal handler
            restore()
            if handler is not None:
                adjustment.disconnect(handler)
                handler = None
        handler = adjustment.connect("changed", on_changed)
        def disconnect_later():
            nonlocal handler
            if handler is not None:
                adjustment.disconnect(handler)
                handler = None
            return False
        GLib.timeout_add(500, disconnect_later)
