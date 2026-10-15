from gi.repository import Gtk, Adw, GLib
from .repository import Repository
from .snapshot_manager import SnapshotManager
from .snapshot import Snapshot
from .project_build import load_project_builds
from .packages_browser_view import PackagesBrowserView


class SnapshotDetailsView(Gtk.Box):
    __gtype_name__ = "SnapshotDetailsView"

    def __init__(self, snapshot: Snapshot, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.snapshot = snapshot
        self.content_navigation_view = content_navigation_view

        self.stack = Adw.ViewStack(vexpand=True)
        switcher = Adw.ViewSwitcher(
            stack=self.stack,
            policy=Adw.ViewSwitcherPolicy.WIDE,
            margin_start=24,
            margin_end=24,
            margin_bottom=12,
        )
        self.append(switcher)
        self.append(self.stack)

        # Snapshot packages.
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                          margin_start=24, margin_end=24, margin_top=12, margin_bottom=24)
        delete_button = Gtk.Button(halign=Gtk.Align.END)
        delete_button.add_css_class("destructive-action")
        delete_button.set_child(Adw.ButtonContent(
            label="Delete snapshot", icon_name="trash-bin-trash-svgrepo-com-symbolic"
        ))
        delete_button.connect("clicked", self.button_delete_clicked)
        details.append(delete_button)
        self.browser = PackagesBrowserView(loader=snapshot.load_ebuilds, loading_title="Loading snapshot…")
        details.append(self.browser)
        self.stack.add_titled_with_icon(
            self._scrolled(details), "packages", "Packages", "archive-minimalistic-svgrepo-com-symbolic"
        )

        # Projects and saved builds that refer to this snapshot.
        self.used_by_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                                   margin_start=24, margin_end=24, margin_top=12, margin_bottom=24)
        self.stack.add_titled_with_icon(
            self._scrolled(self.used_by_box), "used-by", "Used by", "insert-link-symbolic"
        )
        self.stack.connect("notify::visible-child-name", self._on_page_changed)
        self.connect("map", lambda *_: self._load_usage())
        self.browser.load()
        self._load_usage()

    @staticmethod
    def _scrolled(child: Gtk.Widget) -> Gtk.ScrolledWindow:
        return Gtk.ScrolledWindow(
            child=child,
            hexpand=True,
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )

    def _on_page_changed(self, stack, _param):
        if stack.get_visible_child_name() == "used-by":
            self._load_usage()

    def _load_usage(self):
        while child := self.used_by_box.get_first_child():
            self.used_by_box.remove(child)

        projects = Repository.ProjectDirectory.value
        usages = []
        for project in projects:
            rows = []
            try:
                if (project_snapshot := project.get_snapshot()) and project_snapshot.filename == self.snapshot.filename:
                    subtitle = "Uses the latest snapshot" if project.uses_latest_snapshot else "Pinned as the project snapshot"
                    rows.append(("Project snapshot", subtitle))
                for build in load_project_builds(project):
                    snapshot = (((build.details or {}).get("inputs") or {}).get("snapshot") or {}).get("file")
                    if snapshot != self.snapshot.filename:
                        continue
                    date = build.date.strftime("%Y-%m-%d %H:%M")
                    status = build.status.value.replace("_", " ").capitalize()
                    rows.append((build.stage_name, f"Build · {date} · {status}"))
            except Exception as error:
                print(f"Failed to load snapshot usage for project {project.name}: {error}")
            if rows:
                usages.append((project.name, rows))

        if not usages:
            self.used_by_box.append(Adw.StatusPage(
                icon_name="insert-link-symbolic",
                title="No recorded uses",
                description="This snapshot is not selected by a project or referenced by saved stage builds.",
                vexpand=True,
            ))
            return

        for project_name, rows in usages:
            group = Adw.PreferencesGroup(title=GLib.markup_escape_text(project_name))
            for title, subtitle in rows:
                group.add(Adw.ActionRow(
                    title=GLib.markup_escape_text(title),
                    subtitle=GLib.markup_escape_text(subtitle),
                ))
            self.used_by_box.append(group)

    def button_delete_clicked(self, _sender):
        SnapshotManager.shared().remove_snapshot(self.snapshot)
        if hasattr(self, "_window"):
            self._window.close()
        elif self.content_navigation_view is not None:
            self.content_navigation_view.pop()
