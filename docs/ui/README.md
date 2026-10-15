# UI architecture handbook

This handbook describes the GTK 4/libadwaita UI structure and the conventions for extending it. It is aimed at contributors who are adding a main section, detail screen, reusable row, wizard, or long-running operation.

## Pages

- [Core classes and extension points](../architecture/base-classes.md) — persistence, event, Git-directory, operation, and section-registration abstractions.
- [Architecture overview](architecture.md) — application/window hierarchy and the division between UI, domain objects, and operations.
- [Sections and navigation](sections-and-navigation.md) — how main sections are discovered, how pages are pushed or presented, and how to add a section.
- [Layout and window chrome](layout-and-chrome.md) — how to structure page content and place actions in headers and bottom bars.
- [Reusable components](components.md) — shared rows, selectors, status/progress widgets, browsers, and privilege controls.
- [Wizards and operations](wizards-and-operations.md) — setup flows, process lifecycle, progress/log presentation, and deploy/build patterns.
- [Screen and class index](screen-index.md) — a source-oriented map of major screens, reusable classes, and their roles.

## Design conventions

1. Keep the main window as the application shell. Feature sections own their content and their local navigation stack.
2. Prefer existing shared components for repository lists, selection, status, progress, and process output. Add a new shared component when multiple screens need the same behavior.
3. Keep durable work in operation/domain objects; views should observe and present that work rather than own the only copy of its state.
4. Use the navigation event API or a section's supplied `Adw.NavigationView`. The navigation extensions provide the app's standard toolbar and back behavior.
5. Define GTK templates in `.ui` resources and Python behavior in the matching class when that is the established pattern. Register new Python modules in `src/meson.build` and new templates in `src/catalystlab.gresource.xml`.

## Source layout

The relevant implementation is primarily under `src/ui/` and `src/objects/`:

- `src/ui/main_window/` — application shell, sidebar, section content host, and operation list.
- `src/ui/app_sections/` — sidebar destinations and their list/detail entry points.
- `src/ui/components/` — reusable widgets and view components.
- `src/ui/project/`, `src/ui/deploy/`, `src/ui/qemu/`, and other feature folders — feature screens and flows.
- `src/objects/` — application state, repository/domain objects, and long-running processes used by the UI.

The GTK resources are declared in `src/catalystlab.gresource.xml`; the installed Python module list is maintained in `src/meson.build`.
