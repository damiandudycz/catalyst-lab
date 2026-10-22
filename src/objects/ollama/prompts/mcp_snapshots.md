# Snapshot workflows

Use this guide for the Snapshots section and Portage snapshot operations.

## Generate a snapshot

1. Read `create_snapshot`'s schema if it is not already available.
2. Call `list_options` with `categories:["toolsets","runners"]`.
3. Use the exact returned values. The `default` value means the app's default; for a toolset it resolves to the default toolset containing Catalyst, and for a runner it resolves to the app's supported default runner. The UI preselects these defaults.
4. If a required option has exactly one usable value, use it. Ask only if there are multiple choices and no default that resolves the user's request.
5. Call `create_snapshot`. It starts generation and returns an asynchronous status; report that it started and use `list_operations` to follow progress.

## Import a snapshot

`import_snapshot` imports a local `.sqfs` file. Use only an absolute path the user supplied or explicitly selected. The original file is kept. The operation refuses empty files and name collisions; report the returned asynchronous status.

## Inspect or delete a snapshot

- `list_objects(type:"snapshot")` lists Library snapshots.
- `get_snapshot_usage` reports projects and saved build stages that reference a snapshot, like the details page's **Used by** tab.
- `delete_snapshot` is limited to snapshots that are not latest, selected by a project, or used by an active operation. It asks for confirmation; never claim deletion before a successful result.
- Snapshot package browsing uses `list_repository_packages` with the snapshot's exact name or filename.

## Automatic cleanup

`update_settings(setting:"auto_clean_snapshots", value:true)` can immediately remove currently unused snapshots when enabling the preference. The tool displays the affected snapshot names and requires explicit confirmation before enabling it. Do not describe this as a harmless preference toggle.
