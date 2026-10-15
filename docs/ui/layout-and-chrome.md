# Layout and window chrome

This page describes where content and actions belong in CatalystLab views. The aim is to keep screens recognizable across the app and let the navigation shell provide the common window structure.

## Page structure

Most section and detail screens follow this vertical structure:

```text
NavigationPage
└── ToolbarView
    ├── HeaderBar (owned by the navigation shell)
    └── View content
        ├── Scrolled content, when the page can exceed the window
        │   └── vertically stacked content groups
        └── optional fixed bottom action bar
```

The navigation helper creates the toolbar/header wrapper for pushed views. Main section roots receive a similar wrapper from `CatalystlabWindowContent`. A feature view should normally provide its content, not create another `Adw.HeaderBar`, `Adw.ToolbarView`, or `Adw.NavigationPage` around itself.

Use a scrollable content area for long settings, details, lists, and wizards. Keep any action bar outside the scroller when it must remain visible while the user scrolls. Let the main content expand to fill available space; avoid fixed heights for ordinary page content.

## Grouping content

Use `Adw.PreferencesGroup` for a set of related settings or information rows. Give a group a short title that names the concept, and use its description only when users need extra context to make a choice. Keep one subject per group and put groups in a reading order that follows the task: identify/review the item first, configure it next, and show less frequently used or destructive actions later.

Use `Adw.ActionRow` for a concise value, action, selection, or navigation target. Put the main label in the title and supporting/current state in the subtitle. Use `Adw.ExpanderRow` when secondary controls are useful but should not occupy space until expanded. If an interaction needs a large browse/edit surface, make the row a summary and push a dedicated view instead of embedding a large editor in the row.

For pages that do not fit a preferences layout, use a clear vertical hierarchy with section headings, consistent horizontal margins, and spacing between groups. Avoid adding a titled group for a single unrelated control merely to create a card; follow neighboring views where a plain section heading is used.

## Top bars

The navigation shell owns the page title and standard back/navigation behavior. The shell also provides the root-access action in supported contexts. Therefore:

- Do not add a second title bar or duplicate the page title inside the view.
- Put page-specific, frequent actions in the header only when they are compact and useful regardless of scroll position.
- Expose page-specific header controls with `header_bar_end_widgets()`; the navigation wrapper adds them to the standard header. Existing examples include stage/project view modes and QEMU display controls.
- Keep primary multi-step actions such as Continue, Start, Apply, or Finish in the wizard/page action area rather than turning the header into a row of buttons.
- Avoid placing destructive or infrequent actions in the header when they fit better in an Actions group or an overflow menu.

The global shell may show a sidebar toggle in compact layouts. Views should not create a second sidebar control themselves.

## Bottom action bars

Use a fixed bottom bar when users need a persistent action while scrolling, especially for wizard navigation, update review, or a large selection view. Follow the established pattern: a separator above the bar, comfortable horizontal/vertical padding, and a single horizontal row where the leading side holds Back or a secondary action and the trailing side holds the primary action. Centered single actions are appropriate for a result/finish page.

Keep related actions together. Do not split the same step's controls between separate bottom rows or leave a single primary action stranded at an arbitrary edge. Disable actions while their prerequisites are unmet and keep the labels specific to the action.

## Wizard content

Inside `WizardView`, each carousel page is setup content; `WizardView` owns the bottom Back/Next controls and page indicator. Do not add another set of page-navigation buttons inside each page. Put page-specific options in grouped content and report readiness through the wizard's readiness mechanism. The final wizard action starts the operation and switches to the shared process execution view.

## Detail and operation views

Detail pages should make the selected object's identity clear near the top, then group its properties, current status, related resources, and available actions. Put live resource graphs with the usage/status information they explain. Use a dedicated tab or pushed subview for large secondary content such as logs, package browsing, or file contents rather than forcing everything into one long, mixed group.

Operation views should prioritize what is happening now: overall state/progress, current and completed stages, then stage output/logs. Use `MultistageProcessExecutionView` and its stage/output rows so progress, status, and logs remain consistent with other operations.

## Spacing and responsive behavior

- Use the same horizontal margins and inter-group spacing as nearby screens; prefer layout containers and expansion over fixed coordinates.
- Let long content scroll vertically. Avoid horizontal scrolling for standard settings and detail pages.
- Keep text wrapping enabled for descriptive text and use selectable subtitles for values users may need to copy.
- Check the narrow/collapsed-sidebar layout: controls should remain reachable and the view should not assume the sidebar is visible.
- Use separators to mark real changes in function, such as the boundary above a fixed bottom bar, not between every control inside an existing grouped list.

## Before adding a control

Ask what the user is doing at this point in the flow and place the control accordingly:

| Control purpose | Typical placement |
|---|---|
| Navigate to another screen | Activatable `Adw.ActionRow` in the relevant group |
| Common page-level mode/action | Header via `header_bar_end_widgets()` |
| Configure a set of related values | `Adw.PreferencesGroup` with action/expander rows |
| Continue, start, apply, finish | Persistent bottom action bar when the page warrants it |
| Rare or destructive action | Actions group or overflow, with clear confirmation when needed |
| Large editor or browser | Pushed subview with a concise summary row as its entry point |
