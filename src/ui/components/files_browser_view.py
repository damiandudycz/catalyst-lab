import os
from gi.repository import Gtk, Adw, GLib
from .multistage_process_execution_view import AnsiStyle, append_ansi_line
from catalystlab.formatters import format_size
from .scrolled_window import scrolled_window

# ------------------------------------------------------------------------------
# Read-only browser of folder (eg. releng directory): folders and files of folder, opened folders and text files are
# pushed as pages. Nothing can be changed.

_HIDDEN_NAMES = {".git"} # Internal data of Git repository.
_MAX_TEXT_SIZE = 1024 * 1024 # Larger files are shown truncated.

class FilesBrowserView(Gtk.Box):
    """Folders and files of folder inside root (folders first), read when view is loaded."""

    def __init__(self, root: str, path: str | None = None, content_navigation_view: Adw.NavigationView | None = None,
                 title: str | None = None):
        """Title replaces path in title of root folder."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.root = root
        self.title = title
        self.path = path or root
        self.content_navigation_view = content_navigation_view
        self.group = None
        self.loaded = False

    def load(self, force: bool = False):
        """Reads folder (once, unless forced)."""
        if self.loaded and not force:
            return
        self.loaded = True
        if self.group is not None:
            self.remove(self.group)
        relative = os.path.relpath(self.path, self.root)
        title = (self.title or "/") if relative == "." else f"/{relative}"
        self.group = Adw.PreferencesGroup(title=GLib.markup_escape_text(title))
        self.append(self.group)
        try:
            with os.scandir(self.path) as directory:
                entries = [entry for entry in directory if entry.name not in _HIDDEN_NAMES]
        except OSError as e:
            self.group.add(Adw.StatusPage(icon_name="dialog-error-symbolic", title="Folder can't be read",
                                          description=GLib.markup_escape_text(str(e))))
            return
        if not entries:
            self.group.set_description("Empty folder")
            return
        entries.sort(key=lambda entry: (not entry.is_dir(), entry.name.lower()))
        list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        list_box.add_css_class("boxed-list")
        for entry in entries:
            list_box.append(self._row(entry))
        self.group.add(list_box)

    def _row(self, entry: os.DirEntry) -> Adw.ActionRow:
        is_dir = entry.is_dir()
        row = Adw.ActionRow(title=GLib.markup_escape_text(entry.name), activatable=True)
        row.add_prefix(Gtk.Image(icon_name="folder-symbolic" if is_dir else "text-x-generic-symbolic"))
        try:
            if is_dir:
                count = sum(1 for name in os.listdir(entry.path) if name not in _HIDDEN_NAMES)
                row.set_subtitle(f"{count} item{'' if count == 1 else 's'}")
            elif entry.is_file():
                row.set_subtitle(format_size(entry.stat().st_size, max_unit="GB"))
            else:
                row.set_subtitle("Broken link")
        except OSError:
            pass
        if entry.is_symlink():
            row.set_subtitle(f"{row.get_subtitle() or ''} · link to {os.readlink(entry.path)}".lstrip(" ·"))
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        row.connect("activated", lambda row, path=entry.path, is_dir=is_dir: self._open(path, is_dir))
        return row

    def _open(self, path: str, is_dir: bool):
        if self.content_navigation_view is None:
            return
        name = os.path.basename(path)
        if is_dir:
            view = FilesBrowserView(self.root, path, self.content_navigation_view)
            view.set_margin_start(24)
            view.set_margin_end(24)
            view.set_margin_top(12)
            view.set_margin_bottom(24)
            view.load()
            self.content_navigation_view.push_view(scrolled_window(view), title=name)
        else:
            self.content_navigation_view.push_view(FileContentView(path), title=name)

class FileContentView(Gtk.Box):
    """Text of file, read-only. Binary files aren't shown, large ones are truncated."""

    def __init__(self, path: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=24, margin_end=24,
                         margin_top=12, margin_bottom=24)
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as file:
                data = file.read(_MAX_TEXT_SIZE)
        except OSError as e:
            self.append(Adw.StatusPage(icon_name="dialog-error-symbolic", title="File can't be read",
                                       description=GLib.markup_escape_text(str(e)), vexpand=True))
            return
        if b"\0" in data[:8192]:
            self.append(Adw.StatusPage(icon_name="text-x-generic-symbolic", title="Binary file",
                                       description=f"{format_size(size, max_unit='GB')}, contents can't be displayed as text.",
                                       vexpand=True))
            return
        info = Gtk.Label(xalign=0, label=format_size(size, max_unit="GB") + (
            f" · showing first {format_size(_MAX_TEXT_SIZE, max_unit='GB')}" if size > _MAX_TEXT_SIZE else ""))
        info.add_css_class("dimmed")
        info.add_css_class("caption")
        self.append(info)
        text_view = Gtk.TextView(monospace=True, editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.NONE,
                                 top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        # Logs are shown with colors of their terminal output, like output of build steps.
        buffer, style = text_view.get_buffer(), AnsiStyle()
        for line in data.decode("utf-8", errors="replace").splitlines():
            style = append_ansi_line(buffer, line, style)
        scrolled = Gtk.ScrolledWindow(child=text_view, hexpand=True, vexpand=True)
        self.append(Gtk.Frame(child=scrolled))
