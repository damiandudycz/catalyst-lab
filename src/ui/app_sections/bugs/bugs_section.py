import os, hashlib, json, uuid, traceback
from datetime import datetime
from gi.repository import Gtk, Adw, GLib
from .app_section import app_section
from .cl_toggle_group import CLToggle, CLToggleGroup
from .build_failures import collect_package_failures, bugzilla_url, PackageFailure
from .files_browser_view import FileContentView, FilesBrowserView
from .json_serialization import dump_json

# Ways of grouping failures: title, key of group, title of group.
_GROUPINGS = [
    ("Issue", None, None),
    ("Package", lambda failure: failure.atom, lambda failure: failure.atom),
    ("Project", lambda failure: failure.project_directory.id, lambda failure: failure.project_directory.name),
    ("Stage", lambda failure: (failure.project_directory.id, failure.build.stage_name),
     lambda failure: f"{failure.project_directory.name} · {failure.build.stage_name}"),
    ("Date", lambda failure: failure.date.date(), lambda failure: failure.date.strftime("%Y-%m-%d")),
]

@app_section(title="Bugs", icon="bug-svgrepo-com-symbolic", order=7_500)
class BugsSection(Gtk.Box):
    """Packages that failed to build in stages of projects, grouped by package, project, stage or date, with links to
    their bugs in Gentoo Bugzilla and logs of failures."""
    __gtype_name__ = "BugsSection"

    def __init__(self, content_navigation_view: Adw.NavigationView, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, **kwargs)
        self.content_navigation_view = content_navigation_view
        self.failures: list[PackageFailure] = []
        self._issue_notes_cache: dict[str, dict[str, list[dict]]] = {}
        self._expanded: set = set() # Packages of expanded entries, kept when list is shown again.
        self.groups: list[Gtk.Widget] = []
        scrolled_window = Gtk.ScrolledWindow(hexpand=True, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_start=24, margin_end=24,
                               margin_bottom=24)
        scrolled_window.set_child(self.content)
        self.append(scrolled_window)
        # Search and grouping.
        controls = Gtk.Box(spacing=12)
        self.search_entry = Gtk.SearchEntry(hexpand=True, placeholder_text="Search packages, projects, stages...")
        self.search_entry.connect("search-changed", lambda entry: self._show_failures())
        controls.append(self.search_entry)
        self.grouping_toggle = CLToggleGroup(valign=Gtk.Align.CENTER)
        self.grouping_toggle.add_css_class("round")
        self.grouping_toggle.add_css_class("caption")
        for title, _, _ in _GROUPINGS:
            self.grouping_toggle.add(CLToggle(label=title))
        self.grouping_toggle.set_active(0)
        self.grouping_toggle.connect("notify::active", lambda group, _: self._show_failures())
        controls.append(self.grouping_toggle)
        self.content.append(controls)
        # Failures are read again when section is shown (builds could fail meanwhile).
        self.connect("map", lambda widget: self.load())
        self.load()

    def load(self):
        self.failures = collect_package_failures()
        self._show_failures()

    def _show_failures(self):
        for group in self.groups:
            self.content.remove(group)
        self.groups = []
        if not self.failures:
            self._add(Adw.StatusPage(icon_name="bug-svgrepo-com-symbolic", title="No failed packages",
                                     description="Packages that fail to build in stages of projects are listed here."))
            return
        text = self.search_entry.get_text().strip().lower()
        failures = [failure for failure in self.failures if not text or text in self._search_text(failure)]
        if not failures:
            self._add(Adw.StatusPage(icon_name="edit-find-symbolic", title="No results",
                                     description="Try a different search term."))
            return
        grouping, group_key, group_title = _GROUPINGS[max(self.grouping_toggle.get_active(), 0)]
        if grouping in ("Package", "Issue"):
            # Packages are entries of one list; Issue groups are derived below from each failure.
            groups = {None: failures}
        else:
            groups = {}
            for failure in failures: # Newest first, so groups are ordered by their newest failure.
                groups.setdefault(group_key(failure), []).append(failure)
        for items in groups.values():
            newest = items[0]
            if grouping == "Package":
                group = Adw.PreferencesGroup(title="Failed packages")
            elif grouping == "Issue":
                group = Adw.PreferencesGroup(title="Failures grouped by matching cause",
                                             description="Matching uses project, stage, package, repository, phase, and detected reason.")
            else:
                group = Adw.PreferencesGroup(title=GLib.markup_escape_text(group_title(newest)),
                                             description=GLib.markup_escape_text(_failed_times(items)))
            if grouping == "Issue":
                issues: dict[tuple, list[PackageFailure]] = {}
                for failure in items:
                    # Unknown reasons are kept separate: matching just package and phase
                    # could incorrectly merge unrelated failures.
                    key = failure.issue_key or ("unknown", str(failure.build.id), failure.index)
                    issues.setdefault(key, []).append(failure)
                for instances in issues.values():
                    group.add(self._issue_row(instances) if len(instances) > 1 else
                              self._single_issue_row(instances[0]))
                self._add(group)
                continue
            # Failures of the same package are one entry, its instances are shown when it's expanded.
            packages: dict = {}
            for failure in items:
                packages.setdefault(failure.atom, []).append(failure)
            for instances in packages.values():
                group.add(self._row(instances[0], grouping, show_package=True) if len(instances) == 1
                          else self._package_row(instances, grouping))
            self._add(group)

    def _issue_row(self, instances: list[PackageFailure]) -> Adw.ExpanderRow:
        """One confidently matched issue, expandable to its individual build failures."""
        newest = instances[0]
        status = _combined_resolution_status(instances)
        fixed = status == "solved"
        context = f"{newest.project_directory.name} · {newest.build.stage_name}"
        details = [value for value in (_resolution_label(status), newest.phase and f"{newest.phase} phase", newest.reason, context,
                                       _failed_times(instances)) if value]
        row = Adw.ExpanderRow(title=GLib.markup_escape_text(newest.atom),
                              subtitle=GLib.markup_escape_text(" · ".join(details)))
        row.add_prefix(_status_icon(status))
        if fixed:
            row.add_css_class("dimmed")
        row.add_suffix(self._remove_button(instances))
        row.add_suffix(self._fixed_button(instances, fixed))
        row.add_suffix(self._issue_notes_button(newest))
        if newest.is_gentoo:
            row.add_suffix(self._bugzilla_button(newest))
        expanded_key = ("Issue", newest.issue_key)
        row.set_expanded(expanded_key in self._expanded)
        row.connect("notify::expanded", lambda row, _: self._expanded.add(expanded_key) if row.get_expanded()
                    else self._expanded.discard(expanded_key))
        for failure in instances:
            row.add_row(self._row(failure, "Issue", show_package=False))
        return row

    def _single_issue_row(self, failure: PackageFailure) -> Adw.ActionRow:
        """Single failure row with a notes action and direct activation to its details."""
        details = [] if failure.is_gentoo else [f"::{failure.repository}"]
        details.extend((failure.project_directory.name, failure.build.stage_name))
        if failure.phase:
            details.append(f"{failure.phase} phase")
        if failure.reason:
            details.append(failure.reason)
        subtitle = " · ".join(details)
        if failure.resolution_status != "open":
            subtitle = f"{_resolution_label(failure.resolution_status)} · {subtitle}"
        issue_row = Adw.ActionRow(title=GLib.markup_escape_text(failure.package),
                                  subtitle=GLib.markup_escape_text(subtitle), activatable=True)
        issue_row.add_prefix(_status_icon(failure.resolution_status))
        if failure.fixed:
            issue_row.add_css_class("dimmed")
        if failure.is_gentoo:
            issue_row.add_suffix(self._bugzilla_button(failure))
        issue_row.add_suffix(self._issue_notes_button(failure))
        issue_row.add_suffix(self._fixed_button([failure], failure.fixed))
        issue_row.add_suffix(self._remove_button([failure]))
        issue_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        issue_row.connect("activated", lambda *_: self._show_details(failure))
        return issue_row

    @staticmethod
    def _issue_notes_key(failure: PackageFailure) -> str:
        identity = failure.issue_key or (str(failure.project_directory.id), str(failure.build.id), failure.index)
        encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _project_notes_directory(failure: PackageFailure) -> str:
        # Meson installs every module flat into the catalystlab package.
        from .project_build import project_builds_directory
        return os.path.join(project_builds_directory(failure.project_directory), "bugs_notes")

    @classmethod
    def _project_notes_path(cls, failure: PackageFailure, issue_key: str) -> str:
        return os.path.join(cls._project_notes_directory(failure), f"{issue_key}.json")

    def _project_notes(self, failure: PackageFailure) -> dict[str, list[dict]]:
        project_key = str(failure.project_directory.id)
        if project_key not in self._issue_notes_cache:
            directory = self._project_notes_directory(failure)
            notes_by_issue = {}
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(".json"):
                            continue
                        issue_key = entry.name[:-5]
                        try:
                            with open(entry.path, encoding="utf-8") as file:
                                issue_notes = json.load(file)
                            notes_by_issue[issue_key] = self._validated_notes({issue_key: issue_notes}).get(issue_key, [])
                        except (OSError, ValueError) as error:
                            print(f"Failed to read notes for issue {issue_key}: {error}")
            except FileNotFoundError:
                pass
            self._issue_notes_cache[project_key] = notes_by_issue
        return self._issue_notes_cache[project_key]

    @staticmethod
    def _validated_notes(data) -> dict[str, list[dict]]:
        """Keep well-formed issue notes and skip invalid records."""
        if not isinstance(data, dict):
            return {}
        result = {}
        for key, notes in data.items():
            if not isinstance(key, str) or not isinstance(notes, list):
                continue
            valid = []
            for note in notes:
                if (isinstance(note, dict) and isinstance(note.get("id"), str)
                        and isinstance(note.get("created_at"), str) and isinstance(note.get("text"), str)):
                    valid.append(note)
            result[key] = valid
        return result

    def _write_project_notes(self, failure: PackageFailure, notes_by_issue: dict[str, list[dict]]):
        directory = self._project_notes_directory(failure)
        os.makedirs(directory, exist_ok=True)
        for issue_key, notes in notes_by_issue.items():
            if not notes:
                continue
            path = self._project_notes_path(failure, issue_key)
            temporary = path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as file:
                dump_json(file, notes)
                file.write("\n")
            os.replace(temporary, path)

        # Removing the last note for an issue removes its file.
        with os.scandir(directory) as entries:
            for entry in entries:
                if (entry.is_file(follow_symlinks=False) and entry.name.endswith(".json")
                        and entry.name[:-5] not in notes_by_issue):
                    os.remove(entry.path)

    def _issue_notes_button(self, failure: PackageFailure) -> Gtk.Button:
        button = Gtk.Button(icon_name="document-edit-symbolic",
                            tooltip_text=f"View notes across all {failure.atom} issues", valign=Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.connect("clicked", lambda *_: self._show_package_notes(failure))
        return button

    def _show_package_notes(self, failure: PackageFailure):
        view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_start=24, margin_end=24,
                       margin_top=20, margin_bottom=24)
        description = Gtk.Label(label=f"Notes for {failure.atom} across its distinct issues in {failure.project_directory.name}. Showing issues with notes, plus this issue.",
                                xalign=0, wrap=True)
        description.add_css_class("dimmed")
        view.append(description)
        # Rebuild rows inside a regular Gtk.Box. Adw.PreferencesGroup has
        # internal children; removing its first child in a loop can fail to
        # make progress, which blocks opening the notes page before navigation.
        groups_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        loading_group = Adw.PreferencesGroup()
        loading_group.add(Adw.ActionRow(title="Loading issue notes…"))
        groups_box.append(loading_group)
        view.append(groups_box)
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      child=view)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.content_navigation_view = self.content_navigation_view
        page.append(scroller)
        visible_issues: dict[tuple, list[PackageFailure]] = {}

        def render():
            while child := groups_box.get_first_child():
                groups_box.remove(child)
            notes_group = Adw.PreferencesGroup(
                title="Issues",
                description="Each issue is matched by project, stage, package, repository, phase, and detected reason.",
            )
            groups_box.append(notes_group)
            if not visible_issues:
                notes_group.add(Adw.ActionRow(title="No grouped issues",
                                              subtitle="This package has no failures with a stable matching cause yet."))
                return
            for instances in sorted(visible_issues.values(), key=lambda values: values[0].date, reverse=True):
                issue = instances[0]
                notes = self._get_issue_notes(issue)
                heading = " · ".join(value for value in (issue.build.stage_name, issue.repository or "gentoo",
                                                          issue.phase or "Unknown phase") if value)
                subtitle = f"{len(notes)} note{'s' if len(notes) != 1 else ''} · {issue.reason or 'No detected reason'}"
                issue_row = Adw.ExpanderRow(title=GLib.markup_escape_text(heading),
                                            subtitle=GLib.markup_escape_text(subtitle))
                issue_row.add_prefix(_status_icon(_combined_resolution_status(instances)))
                add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add note", valign=Gtk.Align.CENTER)
                add.add_css_class("flat")
                add.connect("clicked", lambda *_args, target=issue: self._edit_issue_note(
                    target, None, refresh=render))
                issue_row.add_suffix(add)
                if not notes:
                    issue_row.add_row(Adw.ActionRow(title="No notes yet",
                                                    subtitle="Add context, investigation steps, or a fix summary."))
                for note in notes:
                    created = note.get("created_at", "")
                    try:
                        date_label = datetime.fromisoformat(created).astimezone().strftime("%Y-%m-%d %H:%M")
                    except (ValueError, TypeError):
                        date_label = ""
                    note_row = Adw.ActionRow(title=date_label or "Note",
                                             subtitle=GLib.markup_escape_text(note.get("text", "")))
                    note_row.set_subtitle_lines(5)
                    edit = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Edit note",
                                      valign=Gtk.Align.CENTER)
                    edit.add_css_class("flat")
                    edit.connect("clicked", lambda *_args, target=issue, note_id=note.get("id"):
                                 self._edit_issue_note(target, note_id, refresh=render))
                    delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete note",
                                        valign=Gtk.Align.CENTER)
                    delete.add_css_class("flat")
                    delete.connect("clicked", lambda *_args, target=issue, note_id=note.get("id"):
                                   self._confirm_delete_issue_note(target, note_id, refresh=render))
                    note_row.add_suffix(edit)
                    note_row.add_suffix(delete)
                    issue_row.add_row(note_row)
                notes_group.add(issue_row)

        if self.content_navigation_view is None:
            print("Cannot open issue notes: content navigation view is unavailable")
            return
        try:
            # Push before loading so an I/O error cannot make the click appear to do nothing.
            self.content_navigation_view.push_view(page, title=f"{failure.atom} notes")
        except Exception:
            traceback.print_exc()
            self._show_notes_error("Could not open the notes page. See the app log for details.")
            return

        def load_and_render():
            nonlocal visible_issues
            try:
                issues: dict[tuple, list[PackageFailure]] = {}
                for item in self.failures:
                    if item.project_directory.id != failure.project_directory.id or item.atom != failure.atom:
                        continue
                    key = item.issue_key or ("unknown", str(item.project_directory.id), str(item.build.id), item.index)
                    issues.setdefault(key, []).append(item)

                # Only display issues with notes plus the issue opened by the user.
                current_key = failure.issue_key or ("unknown", str(failure.project_directory.id),
                                                    str(failure.build.id), failure.index)
                saved_notes = self._project_notes(failure)
                visible_issues = {
                    key: instances for key, instances in issues.items()
                    if key == current_key or self._issue_notes_key(instances[0]) in saved_notes
                }
                render()
            except Exception as error:
                traceback.print_exc()
                while child := groups_box.get_first_child():
                    groups_box.remove(child)
                error_group = Adw.PreferencesGroup(title="Issue notes could not be loaded")
                error_group.add(Adw.ActionRow(
                    title="The notes view opened, but loading its contents failed.",
                    subtitle=f"{type(error).__name__}: {error}",
                ))
                groups_box.append(error_group)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(load_and_render)

    def _show_notes_error(self, message: str):
        root = self.get_root()
        if root is None:
            return
        dialog = Adw.AlertDialog(heading="Issue notes", body=message)
        dialog.add_response("ok", "OK")
        dialog.present(root)

    def _get_issue_notes(self, failure: PackageFailure) -> list[dict]:
        notes = self._project_notes(failure).get(self._issue_notes_key(failure), [])
        return sorted(notes, key=lambda note: note.get("created_at", ""), reverse=True)

    def _save_issue_notes(self, failure: PackageFailure, notes: list[dict], refresh=None):
        notes_by_issue = self._project_notes(failure)
        key = self._issue_notes_key(failure)
        if notes:
            notes_by_issue[key] = notes
        else:
            notes_by_issue.pop(key, None)
        self._write_project_notes(failure, notes_by_issue)
        refresh() if refresh else self._show_failures()

    def _edit_issue_note(self, failure: PackageFailure, note_id: str | None, refresh=None):
        notes = self._get_issue_notes(failure)
        existing = next((note for note in notes if note.get("id") == note_id), None)
        text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False,
                                 top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        text_view.set_size_request(420, 150)
        text_view.get_buffer().set_text(existing.get("text", "") if existing else "")
        scroll = Gtk.ScrolledWindow(child=text_view, min_content_height=150, max_content_height=260,
                                    hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.add_css_class("card")
        dialog = Adw.AlertDialog(heading="Edit issue note" if existing else "Add issue note",
                                 body="Notes are saved with this project and shown newest first.", extra_child=scroll)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def response(_dialog, value):
            if value != "save":
                return
            buffer = text_view.get_buffer()
            content = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True).strip()
            if not content:
                return
            if existing:
                existing["text"] = content
                existing["updated_at"] = datetime.now().astimezone().isoformat()
            else:
                notes.append({"id": str(uuid.uuid4()), "created_at": datetime.now().astimezone().isoformat(),
                              "text": content})
            self._save_issue_notes(failure, notes, refresh=refresh)

        dialog.connect("response", response)
        dialog.present(self.get_root())

    def _confirm_delete_issue_note(self, failure: PackageFailure, note_id: str, refresh=None):
        dialog = Adw.AlertDialog(heading="Delete note?", body="This note will be permanently deleted.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _dialog, response: self._delete_issue_note(failure, note_id, refresh=refresh)
                       if response == "delete" else None)
        dialog.present(self.get_root())

    def _delete_issue_note(self, failure: PackageFailure, note_id: str, refresh=None):
        notes = [note for note in self._get_issue_notes(failure) if note.get("id") != note_id]
        self._save_issue_notes(failure, notes, refresh=refresh)

    def _package_row(self, instances: list[PackageFailure], grouping: str) -> Adw.ExpanderRow:
        """Package that failed more times, expanded to its failures. Its buttons mark all of them."""
        newest = instances[0]
        status = _combined_resolution_status(instances)
        fixed = status == "solved"
        subtitle = " · ".join(value for value in (_resolution_label(status), _failed_times(instances)) if value)
        row = Adw.ExpanderRow(title=GLib.markup_escape_text(newest.atom), subtitle=GLib.markup_escape_text(subtitle))
        row.add_prefix(_status_icon(status))
        if fixed:
            row.add_css_class("dimmed")
        # Suffixes of expander row are added before previous ones: buttons are in the same order as in rows.
        row.add_suffix(self._remove_button(instances))
        row.add_suffix(self._fixed_button(instances, fixed))
        row.add_suffix(self._issue_notes_button(newest))
        if newest.is_gentoo:
            row.add_suffix(self._bugzilla_button(newest))
        expanded_key = (grouping, newest.atom)
        row.set_expanded(expanded_key in self._expanded)
        row.connect("notify::expanded", lambda row, _: self._expanded.add(expanded_key) if row.get_expanded()
                    else self._expanded.discard(expanded_key))
        for failure in instances:
            row.add_row(self._row(failure, grouping, show_package=False))
        return row

    def _add(self, widget: Gtk.Widget):
        self.content.append(widget)
        self.groups.append(widget)

    @staticmethod
    def _search_text(failure: PackageFailure) -> str:
        return " ".join(str(value) for value in (failure.package, failure.repository, failure.phase, failure.reason,
                                                 failure.project_directory.name, failure.build.stage_name)).lower()

    def _row(self, failure: PackageFailure, grouping: str, show_package: bool) -> Adw.ActionRow:
        """Failure of package. Rows of package entries show where it failed instead of package. Rows don't repeat what
        title of group shows."""
        location = {"Project": failure.build.stage_name, "Stage": None}.get(
            grouping, f"{failure.project_directory.name} · {failure.build.stage_name}")
        if show_package:
            title = failure.package
            details = [] if failure.is_gentoo else [f"::{failure.repository}"]
            if location:
                details.append(location)
        else:
            title = location or failure.date.strftime("%Y-%m-%d %H:%M")
            details = [failure.version]
        if failure.phase:
            details.append(f"{failure.phase} phase")
        if title != failure.date.strftime("%Y-%m-%d %H:%M"):
            details.append(failure.date.strftime("%Y-%m-%d %H:%M"))
        subtitle = " · ".join(details)
        if failure.reason:
            subtitle += f"\n{failure.reason}"
        if failure.resolution_status != "open":
            subtitle = f"{_resolution_label(failure.resolution_status)} · {subtitle}"
        row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(subtitle))
        row.add_prefix(_status_icon(failure.resolution_status))
        if failure.fixed:
            row.add_css_class("dimmed")
        if show_package and failure.is_gentoo:
            row.add_suffix(self._bugzilla_button(failure))
        if show_package:
            row.add_suffix(self._issue_notes_button(failure))
        row.add_suffix(self._fixed_button([failure], failure.fixed))
        row.add_suffix(self._remove_button([failure]))
        row.set_activatable(True)
        row.set_tooltip_text("Show details and logs of failure")
        row.connect("activated", lambda _: self._show_details(failure))
        arrow = Gtk.Image(icon_name="go-next-symbolic")
        arrow.add_css_class("dimmed")
        row.add_suffix(arrow)
        return row

    def _fixed_button(self, failures: list[PackageFailure], fixed: bool) -> Gtk.Button:
        button = Gtk.Button(icon_name="check-square-svgrepo-com-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Mark as not fixed" if fixed else "Mark as fixed")
        button.add_css_class("flat")
        if fixed:
            button.add_css_class("success")
        button.connect("clicked", lambda _: self._set_fixed(failures, not fixed))
        return button

    def _set_fixed(self, failures: list[PackageFailure], fixed: bool):
        for failure in failures:
            try:
                failure.set_fixed(fixed)
            except Exception as e:
                print(f"Failed to save state of {failure.package}: {e}")
        self._show_failures()

    def _remove_button(self, failures: list[PackageFailure]) -> Gtk.Button:
        button = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove from list", valign=Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.connect("clicked", lambda _: self._confirm_remove(failures))
        return button

    def _confirm_remove(self, failures: list[PackageFailure]):
        package = failures[0].atom
        body = (f"{len(failures)} failures of {package} will be removed from the list." if len(failures) > 1
                else f"Failure of {failures[0].package} in {failures[0].build.stage_name} will be removed from the list.")
        dialog = Adw.AlertDialog(heading="Remove from list?", body=body + " Builds and their logs are kept.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _, response: self._remove(failures) if response == "remove" else None)
        dialog.present(self.get_root())

    def _remove(self, failures: list[PackageFailure]):
        for failure in failures:
            try:
                failure.remove()
            except Exception as e:
                print(f"Failed to remove {failure.package}: {e}")
        self.load()

    def _bugzilla_button(self, failure: PackageFailure) -> Gtk.Button:
        button = Gtk.Button(icon_name="bug-svgrepo-com-symbolic", tooltip_text=f"Bugs of {failure.atom} in Gentoo Bugzilla",
                            valign=Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.connect("clicked", lambda _: Gtk.UriLauncher.new(bugzilla_url(failure.atom)).launch(self.get_root(), None, None))
        return button

    def _show_details(self, failure: PackageFailure):
        if self.content_navigation_view is not None:
            view = BugDetailsView(failure, self.content_navigation_view, self._show_package_notes)
            self.content_navigation_view.push_view(view, title=failure.package)

class BugDetailsView(Gtk.Box):
    """Failure of package: what failed, its main logs and all data collected for failure (browsed read-only)."""

    def __init__(self, failure: PackageFailure, content_navigation_view: Adw.NavigationView, open_package_notes):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.content_navigation_view = content_navigation_view
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_start=24, margin_end=24,
                          margin_top=12, margin_bottom=24)
        self.append(Gtk.ScrolledWindow(child=content, hexpand=True, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        # Failure:
        info = Adw.PreferencesGroup(title="Failure")
        for title, value in (("Package", failure.package), ("Repository", failure.repository or "gentoo"),
                             ("Project", failure.project_directory.name), ("Stage", failure.build.stage_name),
                             ("Phase", failure.phase), ("Date", failure.date.strftime("%Y-%m-%d %H:%M")),
                             ("Reason", failure.reason),
                             ("State", _resolution_detail(failure.resolution_status) or None)):
            if value:
                row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(value), subtitle_selectable=True)
                row.add_css_class("property")
                info.add(row)
        notes = Adw.ActionRow(title="Issue notes", subtitle="View notes across all issues for this package",
                              activatable=True)
        notes.add_prefix(Gtk.Image(icon_name="document-edit-symbolic"))
        notes.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        notes.connect("activated", lambda *_: open_package_notes(failure))
        info.add(notes)
        content.append(info)
        # Main logs:
        logs = Adw.PreferencesGroup(title="Logs")
        directory = failure.failure_directory
        if directory:
            files = Adw.ActionRow(title="Browse failure files", subtitle="Collected logs and diagnostic files",
                                  activatable=True)
            files.add_prefix(Gtk.Image(icon_name="folder-open-symbolic"))
            files.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            files.connect("activated", lambda *_: self._open_failure_files(directory, failure.package))
            logs.add(files)
        candidates = [("Summary", "Errors of failed packages and ends of their logs", os.path.join(directory, "summary.txt") if directory else None),
                      ("Package build log", "build.log of failed package", os.path.join(directory, failure.package.replace("/", "_"), "build.log") if directory else None),
                      ("Stage build log", "Whole output of catalyst", os.path.join(failure.build.path, "build.log") if failure.build.path else None)]
        for title, subtitle, path in candidates:
            if path and os.path.isfile(path):
                row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
                row.add_prefix(Gtk.Image(icon_name="text-x-generic-symbolic"))
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                row.connect("activated", lambda _, path=path, title=title: content_navigation_view.push_view(FileContentView(path), title=title))
                logs.add(row)
        content.append(logs)

    def _open_failure_files(self, directory: str, package: str):
        browser = FilesBrowserView(root=directory, content_navigation_view=self.content_navigation_view,
                                   title="Collected failure files")
        browser.set_margin_start(24)
        browser.set_margin_end(24)
        browser.set_margin_top(12)
        browser.set_margin_bottom(24)
        browser.load()
        scroller = Gtk.ScrolledWindow(child=browser, hexpand=True, vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.content_navigation_view.push_view(scroller, title=f"{package} files")

def _failed_times(failures: list[PackageFailure]) -> str:
    """Count of failures and date of newest (failures are newest first)."""
    return f"Failed {len(failures)} time{'s' if len(failures) != 1 else ''}, last {failures[0].date.strftime('%Y-%m-%d %H:%M')}"

def _combined_resolution_status(failures: list[PackageFailure]) -> str:
    statuses = {failure.resolution_status for failure in failures}
    if statuses == {"solved"}:
        return "solved"
    if "potentially_resurfaced" in statuses:
        return "potentially_resurfaced"
    if statuses <= {"solved", "potentially_solved"}:
        return "potentially_solved"
    return "open"

def _resolution_label(status: str) -> str:
    return {
        "solved": "Solved",
        "potentially_solved": "Potentially solved",
        "potentially_resurfaced": "Potentially resurfaced",
    }.get(status, "")

def _resolution_detail(status: str) -> str:
    explanation = {
        "potentially_solved": "A later build with the same stage configuration completed without this failure.",
        "potentially_resurfaced": "The same failure appeared again in a later build with the same stage configuration, so the earlier fix may have regressed.",
    }.get(status)
    label = _resolution_label(status)
    return f"{label} — {explanation}" if explanation else label

def _status_icon(status: bool | str) -> Gtk.Image:
    if isinstance(status, bool):
        status = "solved" if status else "open"
    icon_name, css_class = {
        "solved": ("check-square-svgrepo-com-symbolic", "success"),
        "potentially_solved": ("question-square-svgrepo-com-symbolic", "warning"),
        "potentially_resurfaced": ("view-refresh-symbolic", "warning"),
    }.get(status, ("error-box-svgrepo-com-symbolic", "error"))
    icon = Gtk.Image(icon_name=icon_name, pixel_size=24)
    icon.add_css_class(css_class)
    tooltip = {
        "potentially_solved": "A later build with the same stage configuration completed without this failure.",
        "potentially_resurfaced": "The same failure appeared again in a later build with the same stage configuration, so the earlier fix may have regressed.",
    }.get(status)
    if tooltip:
        icon.set_tooltip_text(tooltip)
    return icon
