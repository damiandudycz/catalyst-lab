from __future__ import annotations

import re
from gi.repository import Gtk, Adw, GLib

from .deploy_installation import DeployUser, DEFAULT_USER_GROUPS, USER_GROUPS


class UserRow(Adw.ExpanderRow):
    """Account created on an installed system, with optional group membership."""

    def __init__(self, on_changed, on_remove, available_groups: set[str] | None = None):
        super().__init__(title="New user", icon_name="avatar-default-symbolic")
        self.on_changed = on_changed
        self.name_row = Adw.EntryRow(title="Username")
        self.full_name_row = Adw.EntryRow(title="Full name")
        self.password_row = Adw.PasswordEntryRow(title="Password")
        self.password_confirm_row = Adw.PasswordEntryRow(title="Confirm password")
        for row in (self.name_row, self.full_name_row, self.password_row, self.password_confirm_row):
            row.connect("changed", self._on_changed)
            self.add_row(row)
        self.error_row = Adw.ActionRow(title="")
        self.error_row.add_css_class("warning")
        self.error_row.set_visible(False)
        self.add_row(self.error_row)
        self.group_rows: dict[str, Adw.SwitchRow] = {}
        for group, description in USER_GROUPS:
            row = Adw.SwitchRow(title=group, subtitle=description, active=group in DEFAULT_USER_GROUPS)
            row.connect("notify::active", self._on_changed)
            self.add_row(row)
            self.group_rows[group] = row
        self.groups_row = Adw.EntryRow(title="Other groups")
        self.groups_row.connect("changed", self._on_changed)
        self.add_row(self.groups_row)
        self.set_available_groups(available_groups)
        remove_button = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove user", valign=Gtk.Align.CENTER)
        remove_button.add_css_class("flat")
        remove_button.connect("clicked", lambda _button: on_remove(self))
        self.add_suffix(remove_button)

    def set_available_groups(self, groups: set[str] | None):
        for group, row in self.group_rows.items():
            row.set_visible(groups is None or group in groups)

    def _on_changed(self, *_args):
        user = self.user()
        self.set_title(GLib.markup_escape_text(user.name or "New user"))
        self.set_subtitle(GLib.markup_escape_text(user.full_name))
        self.on_changed()

    def user(self) -> DeployUser:
        groups = [group for group, row in self.group_rows.items() if row.get_visible() and row.get_active()]
        groups += [group for group in re.split(r"[,\s]+", self.groups_row.get_text()) if group]
        return DeployUser(self.name_row.get_text().strip(), self.password_row.get_text(),
                          self.full_name_row.get_text().strip(), list(dict.fromkeys(groups)))

    def error(self) -> str | None:
        user = self.user()
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user.name):
            return "Username must start with a lowercase letter and contain only lowercase letters, digits, - and _"
        if user.name == "root":
            return "Root account already exists"
        if user.password != self.password_confirm_row.get_text():
            return f"Passwords of {user.name} don't match"
        if any(not re.fullmatch(r"[a-z_][a-z0-9_-]*", group) for group in user.groups):
            return f"Groups of {user.name} contain invalid name"
        return None

    def set_error(self, error: str | None):
        self.error_row.set_title(error or "")
        self.error_row.set_visible(error is not None)
