# Reusable UI components

Use these shared classes before inventing another widget for the same job. Check the source and call sites for current parameters and behavior; this page describes roles, not a frozen API contract.

## Repository lists and selection

| Class | Source | Role |
|---|---|---|
| `RepositoryListView` | `src/ui/components/repository_list/repository_list_view.py` | Lists repository-backed objects and coordinates item activation/status. Used by main sections. |
| `ItemRow` | same | Standard action row for a repository item, with common title/subtitle/status presentation. |
| `ItemInstallationRow` | same | Row for an installation associated with a repository item. |
| `ItemSelectionView` | `src/ui/components/item_select/item_select_view.py` | Full selection screen for choosing items from a repository or supplied item set. |
| `ItemSelectionExpanderRow` | `src/ui/components/item_select_expander_row/item_select_expander_row.py` | Embedded expandable selection row, including unavailable/warning states. |
| `StagePackagePickerView` | `src/ui/project/stage_package_picker_view.py` | Stage-specific package browser/picker layered over selectable package sources. |
| `PackagesBrowserView` | `src/ui/components/packages_browser_view.py` | Package tree/browser UI; loading is separated from row interaction. |

Prefer the full selection view for a large browse/edit task, and an expander row for a compact choice embedded in a settings page.

## Status, progress, and operations

| Class/helper | Source | Role |
|---|---|---|
| `StatusIndicatorState`, `StatusIndicator` | `src/ui/components/status_indicator.py` | Visual status marker and state model. |
| `StatusEntry`, `status_values`, `item_status`, `items_status`, `processes_status`, `combined_status`, `unseen_status_entries` | same | Shared aggregation and unseen-state helpers used by section and operation indicators. |
| `UsageGraph` | `src/ui/components/usage_graph.py` | Compact time-series resource graph (for example CPU and memory). |
| `StagesTreeView`, `StagesTreeNode`, `TreeNode`, `StageNodeStatus` | `src/ui/components/stages_tree_view.py` | Project stage hierarchy with status/progress presentation and tree interactions. |
| `MultistageProcessExecutionView` | `src/ui/components/multistage_process_execution_view/` | Renders a process's stages, current activity, output, and completion state. |
| `MultiStageProcessStageRow` | same | Row for a single process stage. |
| `MultiStageProcessStageOutputView`, `AnsiStyle` | same | Stage output/log display with ANSI color handling. |

The operation model lives in `src/objects/global_objects/multistage_process.py`; UI should observe its events/state rather than maintain a second, competing progress model.

## Privileged actions and output

| Class | Source | Role |
|---|---|---|
| `RootAccessButton` | `src/ui/components/root_access_button.py` | Toolbar-level root-access action/status. |
| `RootActionInfoRow` | same | Reusable explanation/action row for a privileged operation. |
| `RootCommandOutputView` | `src/ui/components/root_command_output/root_command_output_view.py` | Presents output from privileged commands. |

Navigation helpers add the standard root-access button in supported contexts. Avoid adding another unrelated root control to a page header without checking how the navigation wrapper handles it.

## Files, layout, and small controls

| Class/helper | Source | Role |
|---|---|---|
| `FilesBrowserView`, `FileContentView` | `src/ui/components/files_browser_view.py` | Read-only file/folder browsing and file content presentation. |
| `preserved_scroll_position` | `src/ui/components/scroll_position/scroll_position.py` | Preserves scroll position while a view's content is refreshed/rebuilt. |
| `CLToggle`, `CLToggleGroup` and fallback classes | `src/ui/components/cl_toggle_group.py` | App-style grouped toggle control, with compatibility fallback implementation. |
| `fit_popup_to_items` | `src/ui/components/combo_row_popup.py` | Sizes combo popups to their item content. |

## Repository row conventions

Repository lists usually show the domain object's identity in the title and concise changing state in the subtitle. Use shared status helpers for consistent running, failed, completed/unseen, or disabled states. Avoid embedding large editors in list rows; push a focused editor or detail view when the interaction needs space.
