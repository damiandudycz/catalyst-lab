import gi
from gi.repository import Gtk, GObject, GLib
from gi.repository import Adw

""" CLToggle and CLToggleGroup represents either Adw.Toggle/ToggleGroup or """
""" Fallback.Toggle/ToggleGroup if LibAdwaita is older. """

class FallbackToggle(Gtk.ToggleButton):
    pass

class FallbackToggleGroup(Gtk.Box):
    __gtype_name__ = 'FallbackToggleGroup'
    __gproperties__ = {
        "active": (int,
            "Active Toggle Index",
            "The index of the active toggle button in the group",
            -1, GLib.MAXINT, -1, GObject.ParamFlags.READWRITE,
        ),
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_orientation(Gtk.Orientation.HORIZONTAL)
        self.add_css_class("linked")
        self._toggles = []
        self._active_index = -1

    def do_get_property(self, prop):
        if prop.name == 'active':
            return self._active_index
        else:
            raise AttributeError(f'unknown property {prop.name}')

    def do_set_property(self, prop, value):
        if prop.name == 'active':
            if 0 <= value < len(self._toggles):
                self._toggles[value].set_active(True)
        else:
            raise AttributeError(f'unknown property {prop.name}')

    def add(self, toggle: Gtk.ToggleButton):
        if not isinstance(toggle, Gtk.ToggleButton):
            raise TypeError("Only Gtk.ToggleButton instances can be added.")
        is_first_toggle = not self._toggles
        index = len(self._toggles)
        self._toggles.append(toggle)
        if not is_first_toggle:
            toggle.set_group(self._toggles[0])
        toggle.connect("toggled", self._on_child_toggled, index)
        self.append(toggle)
        if is_first_toggle:
            toggle.set_active(True)

    def get_active(self) -> int:
        return self.get_property('active')

    def set_active(self, index: int):
        if 0 <= index < len(self._toggles):
            self._toggles[index].set_active(True)
        else:
            raise IndexError(f"Index {index} is out of range for toggle group.")

    def _on_child_toggled(self, button, index):
        """Internal handler to update the active index and emit notify::active."""
        if button.get_active():
            if self._active_index != index:
                self._active_index = index
                self.notify("active")

if hasattr(Adw, 'ToggleGroup') and hasattr(Adw, 'Toggle'):
    CLToggleGroup = Adw.ToggleGroup
    CLToggle = Adw.Toggle
else:
    CLToggleGroup = FallbackToggleGroup
    CLToggle = FallbackToggle

