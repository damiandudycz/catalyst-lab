from gi.repository import Gtk, Adw, GLib

from .combo_row_popup import fit_popup_to_items


class KernelPackageRow(Adw.ComboRow):
    """Shared searchable kernel picker with an inherited/default option and custom package entry."""

    _CUSTOM = object()
    _NOT_SET = object()

    def __init__(self, title, on_select, custom_title="Custom package, eg. =sys-kernel/gentoo-kernel-6.12.8"):
        super().__init__(title=GLib.markup_escape_text(title), enable_search=True)
        # ComboRow only filters searchable models when it knows which item text to match.
        self.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        fit_popup_to_items(self)
        self.on_select = on_select
        self.packages = None
        self.own = None
        self.inherited = None
        self.options = []
        self._building = False
        self._custom_chosen = False
        self.custom_row = Adw.EntryRow(title=custom_title, show_apply_button=True, visible=False)
        self.custom_row.connect("apply", self._on_custom_apply)
        self.connect("notify::selected", self._on_selected)

    def set_packages(self, packages):
        self.packages = [package for package in packages if package.supported]
        self._build()

    def load(self, own, inherited=None):
        self.own, self.inherited = own, inherited
        self._build()

    def selected_atom(self) -> str | None:
        if not 0 <= self.get_selected() < len(self.options):
            return None
        value = self.options[self.get_selected()][0]
        if value is self._CUSTOM:
            return self.custom_row.get_text().strip() or None
        return None if value is self._NOT_SET else value

    def is_custom_selected(self) -> bool:
        return (0 <= self.get_selected() < len(self.options)
                and self.options[self.get_selected()][0] is self._CUSTOM)

    def _build(self):
        known = {package.atom: package for package in self.packages or []}
        options = []
        if self.inherited:
            options.append((None, f"From releng spec: {self.inherited}"))
        elif not self.own:
            options.append((self._NOT_SET, "Not set (catalyst default)"))
        for atom, package in known.items():
            overlays = [repository for repository in package.repositories if repository != "gentoo"]
            options.append((atom, f"{atom} ({', '.join(overlays)})" if overlays else atom))
        if self.packages is None and self.own and self.own not in known:
            options.append((self.own, self.own))
        options.append((self._CUSTOM, "Custom package..."))
        if self.own and self.own in known:
            selected = self.own
        elif self.own and self.packages is not None:
            selected = self._CUSTOM
        elif self.own:
            selected = self.own
        else:
            selected = self._CUSTOM if self._custom_chosen else None if self.inherited else self._NOT_SET
        self._building = True
        labels = [label for _, label in options]
        if labels != [label for _, label in self.options]:
            self.options = options
            self.set_model(Gtk.StringList.new(labels))
        self.set_selected(next((i for i, (value, _) in enumerate(self.options)
                                if value is selected or value == selected), 0))
        self._building = False
        custom = selected is self._CUSTOM
        self.custom_row.set_visible(custom)
        if custom and self.own and self.custom_row.get_text() != self.own:
            self.custom_row.set_text(self.own)
        package = known.get(self.own or self.inherited or "")
        self.set_subtitle(GLib.markup_escape_text(
            package.details if package else "Custom package" if self.own else
            "Catalyst uses sys-kernel/gentoo-kernel (distribution kernel) or sys-kernel/gentoo-sources"
            if not self.inherited else ""))

    def _on_selected(self, *_):
        if self._building or not 0 <= self.get_selected() < len(self.options):
            return
        value = self.options[self.get_selected()][0]
        self._custom_chosen = value is self._CUSTOM
        if value is self._CUSTOM:
            self.custom_row.set_visible(True)
            GLib.idle_add(lambda: self.custom_row.grab_focus() and False)
        elif value is not self._NOT_SET:
            GLib.idle_add(lambda: self.on_select(value, None) and False)

    def _on_custom_apply(self, _row):
        if text := self.custom_row.get_text().strip():
            GLib.idle_add(lambda: self.on_select(text, None) and False)
