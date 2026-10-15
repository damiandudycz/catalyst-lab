# Screen and class index

This is a navigational index, not a complete API reference. Source code is authoritative for constructor arguments and current behavior.

## Main window and sidebar

| Class | Source | Purpose |
|---|---|---|
| `CatalystlabWindow` | `src/ui/main_window/main_window.py` | Global window, section changes, app-wide presentation, breakpoint/sidebar behavior. |
| `PushedViewHandle` | same | Window-like `close()` adapter for a view pushed into global navigation. |
| `CatalystlabWindowContent` | `src/ui/main_window/main_window_content.py` | Creates and hosts a section-local navigation view and the section root toolbar. |
| `CatalystlabWindowSideMenu` | `src/ui/main_window/main_window_side_menu.py` | Main section menu and operation-list host. |
| `MainWindowSideMenuButton` | `src/ui/main_window/main_window_side_menu_button.py` | Sidebar section row and status presentation. |
| `ProcessesList`, `ProcessRow`, `QueuedBuildRow`, `QemuRunRow` | `src/ui/main_window/main_window_processes_list.py` | Operation/queue/VM entries in the sidebar. |

## Main sections

| Section | Source | Main responsibility |
|---|---|---|
| `ProjectsSection` | `src/ui/app_sections/projects/` | Project list and project creation/details entry points. |
| `BuildsSection` | `src/ui/app_sections/builds/` | Build-oriented project list and build history/running build entry points. |
| `EnvironmentsSection` | `src/ui/app_sections/environments/` | Toolsets and build machines. |
| `RelengSection` | `src/ui/app_sections/releng/` | Releng repositories and their detail/update flows. |
| `SnapshotsSection` | `src/ui/app_sections/snapshots/` | Snapshot list, details, and snapshot creation. |
| `OverlaysSection` | `src/ui/app_sections/overlays/` | Overlay repositories and details. |
| `PackagesSection` | `src/ui/app_sections/packages/` | Package directories/repositories. |
| `DeploySection` | `src/ui/app_sections/deploy/` | Deployment entry points. |
| `BugsSection` | `src/ui/app_sections/bugs/` | Build failure/bug list and `BugDetailsView`. |
| `SettingsSection` | `src/ui/app_sections/settings/` | Application settings and library location management. |
| `WelcomeSection` | `src/ui/app_sections/welcome/` | First-run/library setup. |

`AboutSection` and `HelpSection` currently have commented-out `@app_section` decorators and are not active destinations. Template selection is part of the project creation and update flows, not a separate section.

## Feature screens

### Projects and builds

| Class | Source | Purpose |
|---|---|---|
| `ProjectCreateView` | `src/ui/project/project_create_view.py` | Project creation flow. |
| `ProjectDetailsView` | `src/ui/project/project_details_view.py` | Project overview and stage/build navigation. |
| `ProjectStageCreateView` | `src/ui/project/project_stage_create_view.py` | Add/configure a stage. |
| `ProjectStageDetailsView` | `src/ui/project/project_stage_details_view.py` | Edit supported Catalyst stage arguments. Its specialized row classes render text/list/cache/kernel/options values. |
| `StageOptionExpanderRow` | same | Selection-based stage option editor built on `ItemSelectionExpanderRow`. |
| `StageTextEntryRow`, `StageTextSourceRow`, `StageTextEntrySourceRow`, `StageScriptRow`, `StageTextListRow` | same | Text-backed stage values and source/inheritance presentation. |
| `StageCacheRow`, `StageKernelRow`, `KernelPackageRow`, `KernelListRow` | same | Cache and kernel-specific settings. |
| `ProjectBuildView`, `ProjectBuildsView` | `src/ui/project/project_build_view.py`, `project_builds_view.py` | Build setup and build history/details. |
| `ProjectUpdateView`, `ProjectTemplateChooser`, `ProjectTemplateOptionsView` | `src/ui/project/` | Template/repository update and template selection/options flows. |
| `QueuedBuildView`, `QueuedBuildPreview` | `src/ui/project/build_queue_view.py` | Queued build preview and its compact planned-process representation. |

### Repositories and system setup

| Class | Source | Purpose |
|---|---|---|
| `RelengSelectionView`, `RelengCreateView`, `RelengDetailsView` | `src/ui/releng/` | Select, add, and inspect releng repositories. |
| `OverlayCreateView`, `OverlayDetailsView` | `src/ui/overlay/` | Create and inspect overlay repositories. |
| `GitDirectoryCreateConfigView`, `GitDirectoryDetailsView` | `src/ui/git_directory/` | Common Git-backed directory setup/details. |
| `SnapshotCreateView`, `SnapshotDetailsView` | `src/ui/snapshot/` | Create and inspect package snapshots. |
| `ToolsetCreateView`, `ToolsetDetailsView` | `src/ui/toolset/` | Create and inspect toolset environments. |
| `BuildMachineCreateView`, `BuildMachineDetailsView` | `src/ui/build_machine/` | Create and inspect build machines, including resource usage. |
| `PackagesDirectoryDetailsView` | `src/ui/packages/` | Inspect a package directory. |

### Deploy and virtual machines

| Class | Source | Purpose |
|---|---|---|
| `DeployCreateView` | `src/ui/deploy/deploy_create_view.py` | Stage/device deployment setup. `UserRow` and `PartitionRow` are specialized expandable setup rows. |
| `LiveIsoDeployView` | `src/ui/deploy/live_iso_deploy_view.py` | Live ISO deployment/repacking choices. |
| `QemuLaunchWizard` | `src/ui/qemu/qemu_launch_wizard.py` | VM launch configuration wizard. |
| `QemuRunView` | `src/ui/qemu/qemu_run_view.py` | Active VM display, console, controls, and usage presentation. |
| `QemuImagePreparation` and `_StepImage` | same | Multistage disk-image preparation operation and its step. |
| `VncView` | `src/ui/qemu/vnc_view.py` | VNC/display widget integration. |

## Operation/domain class families

Operation implementations are in `src/objects/`, not the screen index above. Common families include:

- `MultiStageProcess` / `MultiStageProcessStage` — shared operation lifecycle and stage model.
- `GitInstallation` / `GitUpdate` with concrete project, releng, overlay, and repository implementations.
- `ProjectStageInstallation`, `ProjectBuild`, and project build process/record classes.
- `ToolsetInstallation` / `ToolsetUpdate`, `BuildMachineInstallation`, and `SnapshotInstallation`.
- `DeployInstallation` and `LiveIsoDeployment`.
- QEMU run/image-preparation objects under `src/objects/qemu/` and `src/ui/qemu/`.

When adding a new screen, follow its operation family: keep reusable work/state in the operation/domain layer and use the UI classes here to configure, launch, observe, and reopen that work.
