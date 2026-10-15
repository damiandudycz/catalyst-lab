# Core classes and extension points

This guide describes the shared classes that provide persistence, events, Git-backed data, and long-running work. These foundations appear throughout the app, so new features should reuse them where they fit.

## Persistent application data

### `Repository[T]`

Source: [`src/objects/global_objects/repository.py`](../../src/objects/global_objects/repository.py)

`Repository` is a typed persistence container for one object or a collection of objects. It stores JSON under the active library's `Config/` directory, loads models through `init_from()`, and writes them through `serialize()`.

Shared repositories are registered in [`repositories.py`](../../src/objects/global_objects/repositories.py) as attributes such as `Repository.ProjectDirectory`, `Repository.Snapshot`, and `Repository.Settings`. Feature code should use these shared instances; creating another repository with an alias already in use raises an error.

- For a single value, supply `default_factory` so missing or unreadable configuration can produce a usable default.
- For a collection, set `collection=True`. The value is a `TrackedList`, which saves after common mutations such as `append`, `remove`, and `clear`.
- Assigning a new list to `.value` wraps it in `TrackedList` and saves it. Assigning a single object also saves immediately.
- Collection entries that fail to deserialize are retained in `broken_items`; saving preserves their raw JSON so damaged entries can be shown and recovered instead of silently discarded.
- Collection JSON uses an `items` array. A single-value repository serializes the object directly.

`Repository` is not a model base class: it owns and persists model instances. Models stored in it normally implement the `Serializable` protocol.

### `Serializable`

`Serializable` is a typing `Protocol` in the same module, not a runtime superclass. An object stored in a repository supplies `serialize() -> dict` and `init_from(data)`. Keep conversion logic next to the model so persistence stays generic.

### Library paths

The same module provides `library_directory()`, `config_directory()`, and `library_path()`. The library is the app's data root; repository JSON lives under its `Config/` directory. The selected library location is stored separately so startup can find it. Use these helpers instead of assembling library paths in feature code.

## Events

### `EventBus[Event]`

Source: [`src/objects/global_objects/event_bus.py`](../../src/objects/global_objects/event_bus.py)

`EventBus` delivers typed enum events to subscribers. By default it schedules callbacks with `GLib.idle_add`, so handlers that update GTK widgets run on the GTK main loop even when an operation emits from a worker thread.

Subscribers are held by weak reference. Keep a strong reference to a callback owner for as long as it must receive events, or the callback may disappear after garbage collection. Use a stable `handle` when code needs to unsubscribe explicitly. Most model events are notifications; read current model state in the callback rather than treating the event arguments as a durable event log.

## Git-backed directories

### `GitDirectory`

Source: [`src/objects/git_directory/git_directory.py`](../../src/objects/git_directory/git_directory.py)

`GitDirectory` is the abstract serializable model for a managed local Git checkout. It provides a UUID, name, Git status and remote metadata, operation/busy tracking, status events, and shared Git helpers.

Subclasses implement `base_location()` and may override `parse_metadata()` when they store subtype-specific metadata. Examples include `ProjectDirectory`, `RelengDirectory`, and `OverlayDirectory`. Project stages are project-specific state; the base class does not parse or manage them.

Changes made to checkout files should emit `GitDirectoryEvent.CONTENT_CHANGED` so status is refreshed. Use the `operation()` context manager around work that should appear as busy in status indicators. It clears the busy state even if the operation raises.

### `GitManager`

Source: [`src/objects/git_directory/git_manager.py`](../../src/objects/git_directory/git_manager.py)

`GitManager` is the abstract manager for one family of `GitDirectory` objects. A subclass implements the class method `repository()` to return its shared repository. `shared()` returns one manager instance per concrete subclass.

The base manager scans the configured base folder, registers discovered directories, prunes records whose folders disappeared, and provides common add/remove/rename behavior. `remove_directory()` recursively deletes the checkout, so use it only for an explicit delete action. Managers can add family-specific behavior, as `ProjectManager` does for stage files and parent links.

## Long-running operations

### `MultiStageProcess`

Source: [`src/objects/global_objects/multistage_process.py`](../../src/objects/global_objects/multistage_process.py)

`MultiStageProcess` is the abstract lifecycle for ordered work such as builds, installs, deploys, updates, snapshots, and image preparation. A subclass implements `setup_stages()` and `complete_process(success)`. The base class owns status, aggregate progress, authorization lifetime, cancellation, cleanup, and process-level events.

- Create stages in `setup_stages()`; each should represent one understandable unit of work.
- Call `start()` to register and execute the process. The next scheduled stage starts after the current stage completes.
- Keep UI views as observers. `started_processes` makes active work discoverable outside its initiating screen; `finished_processes` tracks unseen results.
- `cancel()` cancels the active stage, cleans stages in reverse order, releases authorization, and completes the process as unsuccessful. Cancellation can block while underlying work stops, so call it from a worker when the UI must remain responsive.

Examples include `ProjectBuild`, `DeployInstallation`, `ToolsetInstallation`, and `GitUpdate`.

### `MultiStageProcessStage`

Each stage belongs to one `MultiStageProcess` and provides a name, description, state, progress, output lines, and its own event bus. Implement `start()` to perform work, call `complete(COMPLETED)` or `complete(FAILED)` when done, and implement or extend cancellation/cleanup when the underlying command or server call needs explicit stopping.

`complete()` schedules the parent process to continue on the GLib main loop. Report progress and output through the stage helpers so the shared process view can display them. Cleanup runs in reverse stage order; stages that never started are skipped.

## Main-section registration

### `AppSection` and `@app_section`

Source: [`src/objects/global_objects/app_section.py`](../../src/objects/global_objects/app_section.py)

`AppSection` stores registration metadata for a main destination: section class, title, sidebar label/icon, order, and sidebar visibility. The `@app_section(...)` decorator validates the constructor, attaches `section_details`, and adds the class to the ordered registry.

A section class must accept `__init__(self, content_navigation_view: Adw.NavigationView, **kwargs)`. Use the supplied navigation view for section-local pages. The main-window content host constructs sections and their local navigation stacks from this registry.

## Choosing a foundation

| Need | Use |
|---|---|
| Store settings or durable objects | `Repository` + a `Serializable` model |
| Represent a managed Git checkout | Subclass `GitDirectory` and register its repository |
| Scan/manage a family of Git checkouts | Subclass `GitManager` |
| Run work with visible steps, progress, output, and cancellation | Subclass `MultiStageProcess` and compose `MultiStageProcessStage`s |
| Add a primary sidebar destination | Decorate a section with `@app_section(...)` |
| Notify interested model/UI owners about changes | `EventBus` with an enum event type |
