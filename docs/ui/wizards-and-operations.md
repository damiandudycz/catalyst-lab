# Wizards and long-running operations

## `WizardView`

`WizardView` (`src/ui/components/wizard/wizard_view.py`) is the shared multi-page setup shell. It owns a non-interactive `Adw.Carousel`, a bottom bar with Back/Next controls and page dots, optional welcome-page presentation, and a transition to an installation/process view. The carousel is intentionally non-interactive so users cannot swipe past a page that has not passed its readiness checks.

Wizard subclasses supply pages in GtkBuilder or through the buildable interface, configure page visibility when options change, validate page readiness, and handle the `begin-installation` signal. Use `install_button_label` to name the final action. The optional welcome page is for flows that need an introductory/start screen.

When a wizard is pushed in the main navigation stack, `CatalystlabWindow` connects the system back/Escape behavior to `WizardView.go_back()`: while on setup pages, navigation back advances to the previous wizard page; once the wizard is showing its process/result view, ordinary navigation back can leave the view.

Typical uses include project creation, build setup, deploy setup, VM launch, and repository installation/update flows. A wizard is a presentation flow; the underlying work should be an operation object.

## Process model and execution view

`MultiStageProcess` and `MultiStageProcessStage` in `src/objects/global_objects/multistage_process.py` describe a sequence of work, its stages, state, output, events, cancellation, and completion. Concrete build, install, deploy, update, and image-preparation processes specialize this model.

`MultistageProcessExecutionView` renders that model. It presents the stages and their statuses through `MultiStageProcessStageRow`, and shows output through `MultiStageProcessStageOutputView`. This keeps the same operation understandable from the initiating wizard, a detail screen, or the side menu.

When adding an operation:

1. Represent the work with the appropriate process/domain class and explicit stages.
2. Emit meaningful stage state/progress/output through that model.
3. Present it with the shared execution view rather than building a one-off progress/log implementation.
4. Ensure the owning manager/section keeps the process discoverable if the initiating screen closes.
5. Use the operation status helpers for sidebar/list indicators and unseen completion states.

## Operation list and reopening

`CatalystlabWindowSideMenu` hosts `ProcessesList`. Its row types include `ProcessRow` for generic work, `QueuedBuildRow` for queued builds, and `QemuRunRow` for VM sessions. The operation list is a compact entry point; selecting an item should open a relevant detail/process view rather than duplicating the full operation UI in the sidebar.

`ProcessesList` must remain strongly referenced for its event subscriptions because the event bus stores weak references. The side menu retains it as a child/owned component.

## Feature patterns

- **Builds:** `ProjectBuildView` collects build choices and starts a `ProjectBuild`/stage process; build history/details can reopen records after the run.
- **Deploy:** `DeployCreateView` handles destination/setup choices and deploy installation. `LiveIsoDeployView` is a specialized setup for live ISO configuration, sharing the broader deploy UI conventions.
- **QEMU:** `QemuLaunchWizard` gathers VM settings; `QemuRunView` presents the active run; `QemuImagePreparation` is a multistage process. A QEMU run can remain active while its view is closed and reopened.
- **Repository install/update:** Git-backed installation and update classes are represented as processes, with their relevant repository detail screens providing entry points to run or inspect them.

## Avoiding fragile progress UI

Progress should describe completed work, not just a binary running/finished flag. For multi-stage work, use the actual stage/process model so the UI can calculate and show aggregate progress. A completed or failed process remains a completed operation for progress purposes, while its status communicates the outcome.
