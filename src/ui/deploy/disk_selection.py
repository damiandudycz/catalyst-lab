from gi.repository import Adw, GLib, Gtk


def populate_disk_rows(group, old_rows, disks, on_toggled, *, show_empty=False, disabled_reason=None):
    for row in old_rows:
        group.remove(row)

    rows = []
    selected = None
    for disk in disks:
        reason = disabled_reason(disk) if disabled_reason else None
        subtitle = disk.subtitle + (f", {reason}" if reason else "")
        row = Adw.ActionRow(title=GLib.markup_escape_text(disk.title),
                            subtitle=GLib.markup_escape_text(subtitle))
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        if selected:
            check.set_group(selected)
        else:
            selected = check
        check.connect("toggled", on_toggled, disk)
        row.add_prefix(check)
        row.set_activatable_widget(check)
        row.set_sensitive(not disk.in_use and reason is None)
        group.add(row)
        rows.append(row)

    if not disks and show_empty:
        row = Adw.ActionRow(title="No disks found")
        group.add(row)
        rows.append(row)
    return rows
