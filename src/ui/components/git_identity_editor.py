from gi.repository import Gtk, Adw
from .git_identity import effective_git_identity, system_git_identity
from .repositories import Repository


class GitIdentityEditor(Gtk.Box):
    """Select the host Git config or an app-only author identity."""

    def __init__(self, setup_required: bool = False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.settings = Repository.Settings.value
        self.validity_changed = None
        if setup_required and not system_git_identity().configured and self.settings.git_identity_mode != "custom":
            self.settings.git_identity_mode = "custom"

        group = Adw.PreferencesGroup(
            title="Git identity",
            description="Used when Catalyst Lab creates Git commits. Custom values apply only to Git commands launched by this app."
        )
        self.append(group)

        self.mode_row = Adw.ComboRow(title="Identity source")
        self.mode_row.set_model(Gtk.StringList.new(["System Git configuration", "Custom identity"]))
        self.mode_row.set_selected(1 if self.settings.git_identity_mode == "custom" else 0)
        self.mode_row.connect("notify::selected", self._on_mode_selected)
        group.add(self.mode_row)

        identity = system_git_identity()
        name = identity.name or "Not set"
        email = identity.email or "Not set"
        self.system_row = Adw.ActionRow(title="System Git identity", subtitle=f"{name} · {email}")
        self.system_row.set_subtitle_selectable(True)
        group.add(self.system_row)

        self.name_row = Adw.EntryRow(title="Name")
        self.name_row.set_text(self.settings.git_user_name)
        self.name_row.connect("notify::text", self._on_name_changed)
        group.add(self.name_row)

        self.email_row = Adw.EntryRow(title="Email")
        self.email_row.set_text(self.settings.git_user_email)
        self.email_row.connect("notify::text", self._on_email_changed)
        group.add(self.email_row)

        self.status_label = Gtk.Label(xalign=0, wrap=True)
        self.status_label.set_margin_start(12)
        self.status_label.set_margin_end(12)
        self.status_label.add_css_class("error")
        self.append(self.status_label)
        self._update_visibility()

    def _on_mode_selected(self, row, _parameter):
        self.settings.git_identity_mode = "custom" if row.get_selected() == 1 else "system"
        self._update_visibility()

    def _on_name_changed(self, row, _parameter):
        self.settings.git_user_name = row.get_text()
        self._update_status()

    def _on_email_changed(self, row, _parameter):
        self.settings.git_user_email = row.get_text()
        self._update_status()

    def _update_visibility(self):
        custom = self.settings.git_identity_mode == "custom"
        self.system_row.set_visible(not custom)
        self.name_row.set_visible(custom)
        self.email_row.set_visible(custom)
        self._update_status()

    def _update_status(self):
        identity = effective_git_identity()
        if identity.configured:
            self.status_label.set_text("")
            self.status_label.set_visible(False)
        else:
            missing = identity.missing_description()
            self.status_label.set_text(f"Git {missing} must be set before Catalyst Lab can create commits.")
            self.status_label.set_visible(True)
        if self.validity_changed is not None:
            self.validity_changed(identity.configured)

    def is_valid(self) -> bool:
        return effective_git_identity().configured
