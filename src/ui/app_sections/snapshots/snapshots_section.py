from gi.repository import Gtk, Adw
from .app_section import app_section
from .snapshot_installation import SnapshotInstallation
from .status_indicator import items_status, processes_status
from .snapshot_details_view import SnapshotDetailsView
from .snapshot_create_view import SnapshotCreateView
from .app_events import app_event_bus, AppEvents
from .repository import Repository
from .snapshot_manager import SnapshotManager

@app_section(title="Snapshots", icon="video-frame-svgrepo-com-symbolic", order=5_000)
@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/app_sections/snapshots/snapshots_section.ui')
class SnapshotsSection(Gtk.Box):
    __gtype_name__ = "SnapshotsSection"

    auto_clean_row = Gtk.Template.Child()

    @staticmethod
    def section_status():
        """Snapshot being created (side menu)."""
        return processes_status(SnapshotInstallation)

    def __init__(self, content_navigation_view: Adw.NavigationView, **kwargs):
        super().__init__(**kwargs)
        self.content_navigation_view = content_navigation_view
        self.auto_clean_row.set_active(Repository.Settings.value.auto_clean_snapshots)
        self.auto_clean_row.connect("notify::active", self.on_auto_clean_toggled)

    def on_auto_clean_toggled(self, row, _param):
        if row.get_active() and SnapshotManager.shared().unused_snapshots():
            self._confirm_auto_clean(row)
            return
        Repository.Settings.value.auto_clean_snapshots = row.get_active()

    def _confirm_auto_clean(self, row):
        """Enabling removes unused snapshots right away, they are listed before."""
        names = "\n".join(f"{snapshot.name} ({snapshot.short_details})" for snapshot in SnapshotManager.shared().unused_snapshots())
        dialog = Adw.AlertDialog(heading="Remove unused snapshots?",
                                 body=f"These snapshots are not selected in project configuration, are not the newest, and are not currently in use. They will be removed even if referenced by previous builds or added from file:\n\n{names}")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("enable", "Remove and enable")
        dialog.set_response_appearance("enable", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        def on_response(dialog, response):
            if response == "enable":
                Repository.Settings.value.auto_clean_snapshots = True
            else:
                row.handler_block_by_func(self.on_auto_clean_toggled)
                row.set_active(False)
                row.handler_unblock_by_func(self.on_auto_clean_toggled)
        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    @Gtk.Template.Callback()
    def on_item_row_pressed(self, sender, item):
        self.content_navigation_view.push_view(SnapshotDetailsView(snapshot=item) , title="Snapshot details")

    @Gtk.Template.Callback()
    def on_installation_row_pressed(self, sender, installation):
        app_event_bus.emit(AppEvents.PRESENT_VIEW, SnapshotCreateView(installation_in_progress=installation), "New snapshot", 640, 480)

    @Gtk.Template.Callback()
    def on_add_new_item_pressed(self, sender):
        app_event_bus.emit(AppEvents.PRESENT_VIEW, SnapshotCreateView(), "New snapshot", 640, 480)

