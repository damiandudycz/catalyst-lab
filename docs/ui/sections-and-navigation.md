# Sections and navigation

## Main section registry

`AppSection` in `src/objects/global_objects/app_section.py` is the metadata/registry for main destinations. It is populated by the `@app_section(...)` class decorator. The decorator records title, sidebar label, icon, order, and sidebar visibility, validates the constructor signature, and sorts registered classes by order.

The application imports UI modules through `scan_all_submodules("catalystlab")`; importing a section module executes its decorator and registers the class. The section's Python file still must be included in `src/meson.build`.

Current registered destinations include Projects, Builds, Environments, Releng, Snapshots, Overlays, Packages, Deploy, Bugs, Settings, and Welcome. About and Help classes exist, but their decorators are currently commented out, so they are not registered as destinations. Check the current decorators before relying on a screen being available. Template selection is part of project creation and update, not a separate section.

## Adding a main section

Use a section class with the required constructor signature:

```python
@app_section(title="Example", icon="folder-symbolic", order=6000)
class ExampleSection(Gtk.Box):
    def __init__(self, content_navigation_view: Adw.NavigationView, **kwargs):
        super().__init__(**kwargs)
        self.content_navigation_view = content_navigation_view
```

Then:

1. Put it under `src/ui/app_sections/<feature>/` and use the section's supplied navigation view for details.
2. Set registration metadata deliberately: `order` controls sidebar placement; `show_in_side_bar` controls whether it appears there; `show_side_bar` controls whether the section itself permits the sidebar.
3. Add the Python module to `catalystlab_sources` in `src/meson.build`.
4. If the section uses a GtkBuilder template, add its `.ui` file to `src/catalystlab.gresource.xml` and use a resource path matching the prefix and file path.
5. Optionally implement `section_status()` when the sidebar should show a status indicator for the section.
6. Reuse `RepositoryListView` for repository-backed lists where its row behavior fits.

The constructor annotation is checked at decoration time and must be exactly an `Adw.NavigationView` parameter named `content_navigation_view`, followed by `**kwargs`.

## Navigation choices

There are two navigation layers:

- **Section-local navigation:** `self.content_navigation_view.push_view(view, title=...)` for details and subpages belonging to a section. Back returns to the previous page in that section.
- **Global navigation/presentation:** `app_event_bus.emit(AppEvents.PRESENT_VIEW, view, title, width, height)` for a flow or screen opened from anywhere. `PRESENT_VIEW` follows the user's navigation style: push into the main window or present modally. Use `PUSH_VIEW` only when a global push is explicitly intended.

Navigation helpers are installed by `src/extensions/navigation_view_extensions.py`. The `push_view` extension provides the common page wrapper, toolbar/header, root-access action where applicable, and view-provided header-end widgets. Prefer these helpers over manually constructing a `NavigationPage` for ordinary screens.

For views presented globally, close through the assigned `view._window.close()` handle. In push mode this is a `PushedViewHandle` that pops back to the page beneath the view; in modal mode it is the actual window. `PRESENT_SECTION` can present a section in a standalone navigation context when needed.

## Sidebar and operation entries

`CatalystlabWindowSideMenu` builds the main section buttons from `AppSection.all_sections` and displays the application operation list. `MainWindowSideMenuButton` is the section navigation row and can include the section's status. `ProcessesList` and its `ProcessRow`, `QueuedBuildRow`, and `QemuRunRow` classes represent running or relevant operations independently of the currently selected section.

The side menu is not just a static list of tabs: it also surfaces ongoing work and unseen results. Long-running features should use the shared operation lifecycle and provide a way to reopen useful details after leaving the initiating view.
