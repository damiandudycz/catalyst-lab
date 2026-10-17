from gi.repository import Gtk


def scrolled_window(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    return Gtk.ScrolledWindow(child=child, hexpand=True, vexpand=True,
                              hscrollbar_policy=Gtk.PolicyType.NEVER)
