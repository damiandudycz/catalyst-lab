# UI architecture overview

## Runtime layers

The UI has three broad layers:

1. **Application and window shell** — `CatalystlabApplication` starts the application and `CatalystlabWindow` owns the global navigation view, split sidebar, and active section content.
2. **Sections and screens** — registered main sections provide the primary destinations. Each section creates its own local navigation stack for list and detail pages.
3. **Reusable widgets and operation views** — common rows, selectors, status indicators, progress/log views, and wizard infrastructure are shared by feature screens.

The durable project, build, deploy, Git, VM, and machine state lives in `src/objects/`. UI classes in `src/ui/` initiate operations and observe their state. This keeps a screen from being the only owner of work that should remain visible after navigating away from it.

## Window hierarchy

The top-level template is `src/ui/main_window/main_window.ui`:

```text
CatalystlabApplication
└── CatalystlabWindow (Adw.ApplicationWindow)
    └── main Adw.NavigationView (global pushed pages)
        └── root NavigationPage
            └── Adw.OverlaySplitView
                ├── sidebar NavigationPage
                │   └── ToolbarView
                │       └── CatalystlabWindowSideMenu
                └── CatalystlabWindowContent
                    └── section-local Adw.NavigationView
                        └── section root page
                            └── pushed detail pages
```

The split view collapses at a narrow window width. In collapsed mode the section header can expose the sidebar toggle. A section can also opt out of showing the sidebar using its registration metadata.

`CatalystlabWindowContent.open_app_section()` creates a fresh local `Adw.NavigationView`, constructs the selected section with it, and wraps the section in the standard toolbar/page structure. This means section detail navigation stays local to the section. App-wide pushed views (such as a wizard) use the outer navigation view instead.

## GTK templates and Python classes

Many widgets pair a Python class with a GtkBuilder `.ui` template:

- Python declares the widget type and behavior, commonly with `@Gtk.Template(resource_path=...)`.
- The `.ui` file describes the widget tree, properties, child references, and declarative signal handlers.
- `Gtk.Template.Child()` binds Python attributes to template objects.
- The `.ui` resource path must be included in `src/catalystlab.gresource.xml`.
- A new Python module must be listed in `src/meson.build` so it is installed with the app.

Some reusable widgets build their children in Python instead of having a separate template. Follow the surrounding feature's pattern unless there is a clear reason to choose differently.

## UI and domain ownership

Views are responsible for layout, user input, navigation, and reflecting current state. Domain objects and process classes own data and work that can outlive a particular view. For example, a build is represented by project/build process objects; the project section, build list, operation sidebar, and build details can all present that same work from different angles.

The app event bus (`AppEvents` and `app_event_bus`) carries cross-cutting navigation requests. Use it when a screen needs to open a global section or present a flow without depending directly on `CatalystlabWindow`. Use the supplied section navigation view for local detail pages.

For the shared foundations behind domain objects—especially `Repository`, `GitDirectory`, `GitManager`, and `MultiStageProcess`—see [Core classes and extension points](../architecture/base-classes.md).

The event bus keeps weak subscriber references. A subscriber that must remain active should be held strongly by a long-lived owner; `CatalystlabWindowSideMenu` retains its process-list widget for this reason.
