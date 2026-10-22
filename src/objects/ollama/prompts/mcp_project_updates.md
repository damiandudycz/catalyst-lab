Load when checking or applying updates to a Git- or template-based project.

Use `check_project_source_updates` to inspect availability. Template availability compares the latest template with generated output; Git availability is only the app's cached remote status. Checking does not apply changes.

Use `update_project_template` or `update_project_repository` to review and apply changes in chat. Template updates ask for changed template variables and groups, then resolve project/template conflicts. Git updates fetch upstream and review each conflict. Both preserve saved project commits by default and refuse unsaved edits or unsupported merge histories. Optional `update_overlays` updates the overlays used by project stages first; when omitted and overlays are linked, the tool asks and preselects yes. Report overlay failures even if project update continues.

Set `drop_project_changes=true` only when the user explicitly asks to discard saved project commits. The tool requires a separate in-chat confirmation. Do not use `update_git_directory` on a project's linked overlay unless the user separately asks to update that overlay.
