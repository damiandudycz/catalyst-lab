import threading, uuid, os
from gi.repository import Gtk, Gdk, Adw, GLib, Gio, Pango
from .project_directory import ProjectDirectory
from .project_stage import (
    ProjectStage, ProjectStageEvent,
    load_catalyst_stage_arguments_options, load_catalyst_stage_arguments_options_for_boolean,
    load_catalyst_stage_arguments_details, load_catalyst_stage_automatic_arguments_options,
    apply_default_stage_arguments,
)
from .project_stage_arguments import (
    StageArgumentTargetDetails, StageArgumentOption,
    StageArgumentType, StageArgumentDetails, StageArgumentLevel, stage_argument_level,
    visible_stage_argument_options,
)
from .cl_toggle_group import CLToggle, CLToggleGroup
from .kernel_package_row import KernelPackageRow
from .project_manager import ProjectManager
from .git_directory import GitDirectoryEvent
from .repository_list_view import ItemRow
from .item_select_view import ItemSelectionViewEvent
from .item_select_expander_row import ItemSelectionExpanderRow
from .event_bus import EventBus
from .project_stage_automatic_option import StageAutomaticOption
from .project_stage_argument_descriptions import stage_argument_description
from .project_stage_value_resolver import (
    UNRESOLVED, format_stage_argument_value, resolved_stage_argument_display,
    resolve_stage_argument, split_additions,
)
from .project_stage_argument_serialization import ProjectStageArgumentSerialization
from .project_stage_portage_confdir import (
    StagePortageConfdirSource, stage_overlay_path, stage_root_overlay_path, root_overlay_values, FILE_OVERLAY_ARGUMENTS
)
from .project_stage_cache import CACHE_ARGUMENTS, stage_cache_path
from catalystlab.repository import collapse_home
from .project_stage_kernels import (
    live_cd_kernel_warning, own_kernel_names,
    KERNEL_SETTINGS, KERNEL_SETTING_LEVELS, stage_kernel_names, kernel_setting, own_kernel_settings,
    set_kernel_setting, kernel_name_error, stage_inherits_kernels, releng_kernel_names, set_kernel_names, remove_kernel,
    new_kernel_name, rename_kernel, KernelSettingType, kernel_setting_enabled, releng_kernel_setting
)
from .project_kernel_packages import load_kernel_packages, KernelPackageKind, default_kernel_package, derived_distkernel
from .stage_package_picker_view import StagePackagePickerView, _as_list

_MISSING_STAGE_VALUE = object()


def set_row_description(row: Gtk.Widget, description: str | None):
    """Shows description in tooltip when hovering row. In expander rows it's shown only over their header, not over
    rows inside."""
    target = row
    if isinstance(row, Adw.ExpanderRow):
        def header(widget):
            child = widget.get_first_child()
            while child is not None:
                if isinstance(child, Gtk.Revealer):
                    return None # Rows inside expander.
                if isinstance(child, Adw.ActionRow) or (found := header(child)) is not None:
                    return child if isinstance(child, Adw.ActionRow) else found
                child = child.get_next_sibling()
            return None
        target = header(row) or row
    target.set_tooltip_text(description)

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/project/project_stage_details_view.ui')
class ProjectStageDetailsView(Gtk.Box):
    __gtype_name__ = "ProjectStageDetailsView"

    stage_name_row = Gtk.Template.Child()
    name_used_row = Gtk.Template.Child()
    basic_config_pref_group = Gtk.Template.Child()
    architecture_pref_group = Gtk.Template.Child()
    release_pref_group = Gtk.Template.Child()
    packages_pref_group = Gtk.Template.Child()
    stage_setup_pref_group = Gtk.Template.Child()
    live_image_pref_group = Gtk.Template.Child()
    live_system_pref_group = Gtk.Template.Child()
    build_behavior_pref_group = Gtk.Template.Child()
    other_pref_group = Gtk.Template.Child()
    kernels_pref_group = Gtk.Template.Child()
    row_warnings_group = Gtk.Template.Child()
    row_warnings_row = Gtk.Template.Child()
    toolset_footer_label = Gtk.Template.Child()
    toolset_footer_button = Gtk.Template.Child()
    unsupported_options_group = Gtk.Template.Child()

    # Basic or advanced settings (STAGE_ARGUMENT_LEVELS), last choice is used by stages opened later while app runs.
    advanced_default = False

    def __init__(self, project_directory: ProjectDirectory, stage: ProjectStage, content_navigation_view: Adw.NavigationView | None = None):
        super().__init__()
        self.project_directory = project_directory
        self.stage = stage
        self.content_navigation_view = content_navigation_view
        self.advanced = ProjectStageDetailsView.advanced_default
        self.stage_toolsets = []
        self.active_stage_toolset = None
        self._unsupported_option_rows = []
        self.template_stage_baseline = {}
        self.stage_override_metadata = {}
        self._name_is_used = False
        self._setup_mode_toggle()
        self.connect("realize", self.on_realize)
        # Reapply the selected mode once the page is mapped. Rows are populated during
        # realize; this second pass ensures the initial frame uses the mode shown by the
        # header toggle, including rows whose widgets finish setup during realization.
        self.connect("map", self._on_map)
        self.project_directory.event_bus.subscribe(
            GitDirectoryEvent.CONTENT_RELOADED, self._on_project_contents_reloaded
        )

    # Basic and advanced mode
    # --------------------------------------------------------------------------

    def _setup_mode_toggle(self):
        """Basic / Advanced / Customized toggle in the header bar."""
        self.mode_toggle = CLToggleGroup(valign=Gtk.Align.CENTER)
        self.mode_toggle.add_css_class("round")
        self.mode_toggle.add_css_class("caption")
        self.mode_toggle.add(CLToggle(label="Basic"))
        self.mode_toggle.add(CLToggle(label="Advanced"))
        self.mode_toggle.add(CLToggle(label="Customized"))
        self.mode_toggle.set_active(1 if self.advanced else 0)
        self.mode_toggle.connect("notify::active", self._on_mode_changed)
        self.no_changed_settings_row = Adw.ActionRow(title="No settings differ from the template or defaults")
        self.no_changed_settings_row.add_css_class("dim-label")
        self.no_changed_settings_row.set_visible(False)
        self.basic_config_pref_group.add(self.no_changed_settings_row)
        self._attach_restore_default_button(self.stage_name_row, StageArgumentDetails.name.value)

    def header_bar_end_widgets(self) -> list[Gtk.Widget]:
        return [self.mode_toggle]

    def _on_mode_changed(self, group, _param):
        mode = group.get_active()
        self.advanced = mode != 0
        if mode != 2:
            ProjectStageDetailsView.advanced_default = self.advanced
        self.apply_mode()

    def _on_map(self, _widget):
        self.advanced = self.mode_toggle.get_active() != 0
        self.apply_mode()

    def apply_mode(self):
        """Shows rows of current mode, and groups which have any of them."""
        changed_only = self.mode_toggle.get_active() == 2
        for row in getattr(self, "configuration_rows", []):
            changed = self._is_stage_argument_changed(row.argument.attribute_name)
            _set_customized_style(row, changed)
            visible = (changed if changed_only else
                       self.advanced or row.level == StageArgumentLevel.BASIC)
            row.set_visible(visible)
            if getattr(row, "restore_default_button", None):
                row.restore_default_button.set_visible(changed)
        changed_configuration = any(
            self._is_stage_argument_changed(row.argument.attribute_name)
            for row in self.configuration_rows
        )
        changed_basic_configuration = any(
            row.pref_group is self.basic_config_pref_group
            and self._is_stage_argument_changed(row.argument.attribute_name)
            for row in self.configuration_rows
        )
        name_changed = self._is_stage_argument_changed(StageArgumentDetails.name.value)
        _set_customized_style(self.stage_name_row, name_changed)
        self.stage_name_row.set_visible(not changed_only or name_changed)
        self.stage_name_row.restore_default_button.set_visible(name_changed)
        self.name_used_row.set_visible(not changed_only and self._name_is_used)
        changed_kernel_settings = False
        changed_kernel_toggle = False
        for row in getattr(self, "kernel_rows", []):
            if isinstance(row, StageKernelRow):
                _set_customized_style(row, row.has_stage_overrides())
                row.set_advanced(self.advanced, changed_only)
                changed_kernel_settings |= row.has_stage_overrides()
                own = own_kernel_settings(self.stage).get(row.kernel_name, {})
                for setting, setting_row in row.entry_rows:
                    _set_customized_style(setting_row, setting.key in own)
                _set_customized_style(row.name_row, bool(own))
            elif getattr(row, "changed_filter_key", None):
                changed = self._is_stage_argument_changed(row.changed_filter_key)
                _set_customized_style(row, changed)
                row.set_visible(not changed_only or changed)
                if getattr(row, "restore_default_button", None):
                    row.restore_default_button.set_visible(changed)
                changed_kernel_toggle |= changed
            elif getattr(row, "is_kernel_add_action", False):
                row.set_visible(not changed_only)
        self.no_changed_settings_row.set_visible(
            changed_only and not name_changed and not changed_kernel_settings and not changed_configuration
        )
        if changed_kernel_toggle:
            self.no_changed_settings_row.set_visible(False)
        groups = (self.architecture_pref_group, self.release_pref_group, self.packages_pref_group,
                  self.stage_setup_pref_group, self.live_image_pref_group,
                  self.live_system_pref_group, self.build_behavior_pref_group, self.other_pref_group)
        for group in groups:
            # Determine visibility from the stage arguments and selected mode, rather than GTK's
            # effective child visibility. That value can be false while the group is hidden and
            # then incorrectly keep the group empty when switching back to Advanced.
            has_visible_rows = any(
                row.pref_group is group
                and (self._is_stage_argument_changed(row.argument.attribute_name) if changed_only else
                     self.advanced or row.level == StageArgumentLevel.BASIC)
                for row in self.configuration_rows
            )
            group.set_visible(has_visible_rows)
        self.basic_config_pref_group.set_visible(
            not changed_only or name_changed or changed_basic_configuration or self.no_changed_settings_row.get_visible()
        )
        self.kernels_pref_group.set_visible(
            getattr(self, "boot_kernel_argument", None) is not None
            and (not changed_only or changed_kernel_settings or changed_kernel_toggle)
        )

    def on_realize(self, widget):
        self.get_root().set_focus(None)
        self._window_active_handler = self.get_root().connect("notify::is-active", self._on_window_active_changed)
        self.connect("unrealize", self._on_unrealize)
        self.load_stage_details()
        self.load_configuration_rows()
        self.monitor_information_changes()

    # Loading stage data
    # --------------------------------------------------------------------------

    def load_stage_details(self, reload_toolset: bool = True):
        self.stage_name_row.set_text(self.stage.name)
        set_row_description(self.stage_name_row, stage_argument_description(StageArgumentDetails.name.value))
        from .project_template_update import project_stage_baseline
        from catalystlab import project_stage_overrides
        project_path = self.project_directory.directory_path()
        self.template_stage_baseline = project_stage_baseline(project_path, self.stage.id)
        self.stage_override_metadata = project_stage_overrides.get(project_path, self.stage.id)
        if reload_toolset:
            self._load_stage_toolset()

    def _on_project_contents_reloaded(self, _project):
        """Replace cached stage state and editors after discard or a successful Git update."""
        stage = next((item for item in self.project_directory.stages if item.id == self.stage.id), None)
        if stage is None:
            if self.content_navigation_view is not None:
                self.content_navigation_view.pop()
            return
        self.stage = stage
        self.load_stage_details(reload_toolset=False)
        self.load_configuration_rows()
        if hasattr(self, "_page"):
            self._page.set_title(self.stage.name)

    def _load_stage_toolset(self):
        from .toolset_selection import sorted_toolsets, toolset_has_applications, default_toolset
        from .toolset_application import ToolsetApplication
        from catalystlab import project_stage_toolsets
        available = sorted_toolsets()
        self.stage_toolsets = [toolset for toolset in available
                               if toolset_has_applications(toolset, [ToolsetApplication.CATALYST])
                               and os.path.isfile(toolset.file_path())]
        project_path = self.project_directory.directory_path()
        saved_toolset_id = project_stage_toolsets.get(project_path, self.stage.id)
        remembered = next((item for item in self.stage_toolsets if str(item.uuid) == str(saved_toolset_id)), None)
        fallback = default_toolset(applications=[ToolsetApplication.CATALYST], toolsets=self.stage_toolsets)
        self.active_stage_toolset = remembered or fallback or (self.stage_toolsets[0] if self.stage_toolsets else None)
        name = self.active_stage_toolset.name if self.active_stage_toolset else "No Catalyst toolset available"
        self.toolset_footer_label.set_label(f"Catalyst toolset · {name}")
        self.toolset_footer_button.set_sensitive(bool(self.stage_toolsets))
        if self.active_stage_toolset:
            project_stage_toolsets.set(project_path, self.stage.id, str(self.active_stage_toolset.uuid))
        if self.active_stage_toolset:
            try:
                apply_default_stage_arguments(
                    project_directory=self.project_directory,
                    stage=self.stage,
                    toolset=self.active_stage_toolset,
                )
            except Exception as error:
                print(f"Failed to set missing defaults for {self.stage.name}: {error}")
        # Defaults are only an in-memory starting point while viewing the stage.
        # A toolset preference alone must never dirty tracked project data.

    def _is_stage_argument_changed(self, name: str) -> bool:
        """Whether this value differs from template/default, or was explicitly chosen by the user."""
        current = self.stage.serialize().get(name, _MISSING_STAGE_VALUE)
        metadata = self.stage_override_metadata
        # Explicit edits take precedence over equality with the template: saving the same value is meaningful because
        # it opts this stage out of following future template changes.
        if name in metadata.get("explicit", []):
            return True
        template_value = self.template_stage_baseline.get(name, _MISSING_STAGE_VALUE)
        if template_value is not _MISSING_STAGE_VALUE:
            return current != template_value
        default_value = metadata.get("defaults", {}).get(name, _MISSING_STAGE_VALUE)
        if default_value is _MISSING_STAGE_VALUE:
            runtime_default = getattr(self.stage, "_default_stage_arguments", {}).get(name, _MISSING_STAGE_VALUE)
            if runtime_default is not _MISSING_STAGE_VALUE:
                default_value = ProjectStageArgumentSerialization.serialize(runtime_default)
        if default_value is not _MISSING_STAGE_VALUE:
            # Runtime toolset defaults are deliberately not serialized into stage.json.
            # Compare the effective in-memory value, while preserving an explicit
            # same-as-default edit through the sidecar marker above.
            effective_value = getattr(self.stage, name, _MISSING_STAGE_VALUE)
            if effective_value is _MISSING_STAGE_VALUE:
                return False
            return ProjectStageArgumentSerialization.serialize(effective_value) != default_value
        if name in {"id", "parent", "target", "name", "releng_template"}:
            # These are initial stage identity/selection values for a fresh project,
            # rather than edits to a generated Catalyst setting.
            return False
        return current is not _MISSING_STAGE_VALUE

    def _attach_restore_default_button(self, row, name: str):
        button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Restore template or default value",
                            valign=Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.connect("clicked", self._restore_stage_argument, name)
        row.add_suffix(button)
        row.restore_default_button = button

    def _restore_stage_argument(self, _button, name: str):
        from catalystlab import project_stage_overrides
        template_value = self.template_stage_baseline.get(name, _MISSING_STAGE_VALUE)
        manager = ProjectManager.shared()
        project_path = self.project_directory.directory_path()
        if template_value is not _MISSING_STAGE_VALUE:
            value = ProjectStageArgumentSerialization.deserialize(template_value)
            if name == StageArgumentDetails.name.value:
                if value != self.stage.name:
                    manager.rename_stage(project=self.project_directory, stage=self.stage, name=value)
            else:
                manager.change_stage_argument(
                    project=self.project_directory, stage=self.stage, argument=name, value=value
                )
            project_stage_overrides.mark_explicit(project_path, self.stage.id, name, explicit=False)
        else:
            metadata_default = self.stage_override_metadata.get("defaults", {}).get(name, _MISSING_STAGE_VALUE)
            default_value = (ProjectStageArgumentSerialization.deserialize(metadata_default)
                             if metadata_default is not _MISSING_STAGE_VALUE else _MISSING_STAGE_VALUE)
            if name == StageArgumentDetails.name.value and default_value is not _MISSING_STAGE_VALUE:
                if default_value != self.stage.name:
                    manager.rename_stage(project=self.project_directory, stage=self.stage, name=default_value)
                project_stage_overrides.mark_explicit(project_path, self.stage.id, name, explicit=False)
            else:
                manager.remove_stage_argument(
                    project=self.project_directory, stage=self.stage, argument=name
                )
            self.stage._default_stage_arguments.pop(name, None)
            if default_value is not _MISSING_STAGE_VALUE and default_value is not None and name != StageArgumentDetails.name.value:
                setattr(self.stage, name, default_value)
                if name in {"id", "parent", "target", "releng_template"}:
                    manager.save_stage(project=self.project_directory, stage=self.stage)
                else:
                    self.stage._default_stage_arguments[name] = default_value
            elif self.active_stage_toolset:
                try:
                    apply_default_stage_arguments(
                        project_directory=self.project_directory, stage=self.stage,
                        toolset=self.active_stage_toolset,
                    )
                except Exception as error:
                    print(f"Failed to restore default for {name}: {error}")
        self.stage_override_metadata = project_stage_overrides.get(project_path, self.stage.id)
        self.load_configuration_rows()

    @Gtk.Template.Callback()
    def _on_change_toolset_clicked(self, button):
        dialog = Adw.Dialog(
            title="Change Catalyst toolset",
            content_width=420,
            presentation_mode=Adw.DialogPresentationMode.FLOATING,
        )
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_top=18, margin_bottom=12, margin_start=18, margin_end=18)
        dialog.set_child(content)
        content.append(Gtk.Label(
            label="Choose a toolset to reload the available stage settings.",
            wrap=True,
            xalign=0,
            css_classes=["dim-label"],
        ))

        scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      max_content_height=320, propagate_natural_height=True)
        list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                               css_classes=["boxed-list", "toolset-chooser"],
                               margin_top=4, margin_bottom=4, margin_start=4, margin_end=4)
        scroller.set_child(list_box)
        content.append(scroller)

        def choose_toolset(_list, row):
            toolset = row.toolset
            self.active_stage_toolset = toolset
            from catalystlab import project_stage_toolsets
            project_stage_toolsets.set(
                self.project_directory.directory_path(), self.stage.id, str(toolset.uuid)
            )
            try:
                apply_default_stage_arguments(
                    project_directory=self.project_directory,
                    stage=self.stage,
                    toolset=toolset,
                )
            except Exception as error:
                print(f"Failed to set missing defaults for {self.stage.name}: {error}")
            # Persist the toolset preference only. Any stage values are saved when
            # the user explicitly changes a setting.
            self.toolset_footer_label.set_label(f"Catalyst toolset · {toolset.name}")
            self.load_configuration_rows()
            dialog.close()

        list_box.connect("row-activated", choose_toolset)
        from .toolset_selection import default_toolset
        default = default_toolset(toolsets=self.stage_toolsets)
        for toolset in self.stage_toolsets:
            versions = toolset.short_details
            row = Adw.ActionRow(title=toolset.name, subtitle=versions or "Catalyst version unavailable",
                                activatable=True)
            row.add_prefix(Gtk.Image.new_from_icon_name("toolbox-symbolic"))
            if toolset is default:
                row.add_css_class("default-toolset-row")
            row.toolset = toolset
            list_box.append(row)

        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        cancel = Gtk.Button(label="Cancel", hexpand=True)
        cancel.connect("clicked", lambda *_: dialog.close())
        actions.append(cancel)
        content.append(actions)
        dialog.present(self.get_root())

    def load_configuration_rows(self):
        # Reset configuration rows
        if hasattr(self, 'configuration_rows'):
            for row in self.configuration_rows:
                row.pref_group.remove(row)
        self.configuration_rows = []
        self._row_load_warnings = []
        self.boot_kernel_argument = None
        for row in self._unsupported_option_rows:
            row.pref_group.remove(row)
        self._unsupported_option_rows = []
        # Load arguments rows
        toolset = self.active_stage_toolset
        arguments_details = {}
        metadata_available = False
        try:
            if toolset is None:
                from .toolset_selection import default_toolset
                toolset = default_toolset()
            if toolset is not None:
                arguments_details = load_catalyst_stage_arguments_details(
                    toolset=toolset,
                    target_name=self.stage.target
                )
                metadata_available = True
        except Exception as error:
            self._row_load_warnings.append(f"Could not read Catalyst settings: {self._short_error(error)}")
        supported_names = {argument.attribute_name for argument in arguments_details.values()}
        explicit_none = getattr(self.stage, "_explicit_none_arguments", set())
        structural = {
            "event_bus", "_explicit_none_arguments", "_default_stage_arguments",
            "_explicit_stage_arguments", "id", "parent", "target", "name", "releng_template",
        }
        stored_names = {
            name for name, value in self.stage.__dict__.items()
            if name not in structural and (value is not None or name in explicit_none)
        }
        missing_names = stored_names - supported_names
        for name in sorted(missing_names):
            details = StageArgumentDetails.named(name)
            if details is not None:
                arguments_details[name] = StageArgumentTargetDetails(name=name, required=False, details=details)
            else:
                arguments_details[name] = StageArgumentTargetDetails(name=name, required=False, details=None)
        for name, arg in arguments_details.items():
            if arg.details == StageArgumentDetails.boot_kernel and metadata_available and name not in missing_names:
                # Edited as list of kernels in Kernels group (load_kernel_rows).
                self.boot_kernel_argument = arg
                continue
            try:
                group = self.pref_group_for_argument(argument=arg)
                if group is None and arg.details != StageArgumentDetails.name:
                    group = self.other_pref_group
                if group:
                    if arg.details in (StageArgumentDetails.snapshot_treeish, StageArgumentDetails.source_subpath):
                        row = self._create_saved_value_row(
                            name, self._resolved_or_stored_value(arg.attribute_name), "Managed by Catalyst Lab"
                        )
                    elif not metadata_available:
                        value = self._resolved_or_stored_value(arg.attribute_name)
                        row = self._create_saved_value_row(name, value, "Support not verified: no Catalyst toolset available")
                    elif arg.details == StageArgumentDetails.boot_kernel and name in missing_names:
                        row = self._create_saved_value_row(
                            name, self._resolved_or_stored_value(arg.attribute_name),
                            "Not supported by the selected Catalyst toolset",
                        )
                    elif arg.details is None:
                        row = self._create_saved_value_row(
                            name, self._resolved_or_stored_value(arg.attribute_name),
                            "Not recognized by the selected Catalyst toolset",
                        )
                    else:
                        try:
                            row = self.create_row_for_argument(argument=arg, toolset=toolset)
                        except Exception as error:
                            self._row_load_warnings.append(f"{arg.display_name}: {self._short_error(error)}")
                            row = self._create_saved_value_row(
                                name, self._resolved_or_stored_value(arg.attribute_name),
                                "Could not load editor for the selected Catalyst toolset",
                            )
                    if name in missing_names:
                        self._mark_saved_setting_unavailable(row, name, metadata_available)
                    set_row_description(row, stage_argument_description(arg.name))
                    row.pref_group = group
                    row.level = stage_argument_level(arg.details)
                    row.argument = arg
                    if not hasattr(row, "event_bus"):
                        row.event_bus = EventBus[ItemSelectionViewEvent]()
                    row.event_bus.subscribe(ItemSelectionViewEvent.ITEM_CHANGED, self.argument_changed, self)
                    row.pref_group.add(row)
                    self._attach_restore_default_button(row, name)
                    self.configuration_rows.append(row)
                    # Kernels depend on boot/kernel and releng template, refreshed after argument is saved.
                    row.event_bus.subscribe(ItemSelectionViewEvent.ITEM_CHANGED, self.load_kernel_rows, "kernels")
            except Exception as error:
                # A single option editor (for example, a select list whose source is
                # unavailable) must not prevent later Catalyst settings from loading.
                self._row_load_warnings.append(f"{arg.display_name}: {self._short_error(error)}")
        try:
            self.load_kernel_rows()
        except Exception as error:
            self._row_load_warnings.append(f"Kernels: {self._short_error(error)}")
        self._update_row_load_warnings()
        self.apply_mode()

    def _create_saved_value_row(self, name: str, value, note: str | None = None) -> Adw.ActionRow:
        """Read-only fallback that keeps saved settings visible without Catalyst metadata."""
        details = StageArgumentDetails.named(name)
        title = details.display_name if details else name
        if value is UNRESOLVED:
            stored = getattr(self.stage, details.name if details else name, None)
            subtitle = self._format_stored_value(stored) or "Saved value could not be resolved"
        elif value is None:
            subtitle = "(None)"
        else:
            subtitle = self._format_stored_value(value) or "(empty)"
        if note:
            subtitle = f"{subtitle} · {note}"
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        return row

    def _remove_unsupported_setting(self, _button, name: str):
        ProjectManager.shared().remove_stage_argument(
            project=self.project_directory,
            stage=self.stage,
            argument=name,
        )
        self.load_configuration_rows()

    def _mark_saved_setting_unavailable(self, row, name: str, metadata_available: bool):
        if metadata_available:
            message = "This saved setting is not supported by the selected Catalyst toolset. Its value is preserved."
            button_label = "Remove unsupported saved setting"
        else:
            message = "Catalyst support cannot be verified because no toolset is available. This saved value is preserved."
            button_label = "Remove saved setting"
        warning_icon = Gtk.Image.new_from_icon_name("danger-triangle-svgrepo-com-symbolic")
        warning_icon.add_css_class("warning")
        warning_icon.set_valign(Gtk.Align.CENTER)
        if hasattr(row, "add_prefix"):
            row.add_prefix(warning_icon)
        row.set_tooltip_text(message)
        remove_button = Gtk.Button(
            icon_name="user-trash-symbolic",
            tooltip_text=button_label,
            valign=Gtk.Align.CENTER,
        )
        remove_button.add_css_class("flat")
        remove_button.connect("clicked", self._remove_unsupported_setting, name)
        row.add_suffix(remove_button)

    def _resolved_or_stored_value(self, name: str):
        """Prefer the effective value, but never hide an explicitly stored value."""
        value = resolve_stage_argument(self.project_directory, self.stage, name)
        if value is UNRESOLVED or value is None:
            return getattr(self.stage, name) if hasattr(self.stage, name) else value
        return value

    @staticmethod
    def _format_stored_value(value) -> str | None:
        if isinstance(value, StageAutomaticOption):
            return {
                StageAutomaticOption.INHERIT_FROM_PARENT: "Inherit from parent",
                StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE: "Inherit from Releng template",
                StageAutomaticOption.GENERATE_AUTOMATICALLY: "Generate automatically",
            }.get(value, value.name.replace("_", " ").title())
        if isinstance(value, list):
            return ", ".join(ProjectStageDetailsView._format_stored_value(item) or "" for item in value)
        return format_stage_argument_value(value)

    @staticmethod
    def _short_error(error: Exception) -> str:
        message = str(error).strip() or type(error).__name__
        return message if len(message) <= 240 else message[:237] + "..."

    def _update_row_load_warnings(self):
        warnings = getattr(self, "_row_load_warnings", [])
        self.row_warnings_row.set_subtitle("\n".join(warnings))
        self.row_warnings_group.set_visible(bool(warnings))

    def load_kernel_rows(self, *args):
        """Kernels of stage (boot/kernel of catalyst, list of names) with settings of each (boot/kernel/<name>/...).
        Kernels are added and removed one by one. Stage uses kernels of releng template until it changes them.
        Expanded kernels stay expanded."""
        try:
            self._load_kernel_rows(*args)
            self._row_load_warnings = [
                warning for warning in getattr(self, "_row_load_warnings", [])
                if not warning.startswith("Kernels:")
            ]
        except Exception as error:
            self._row_load_warnings = [
                warning for warning in getattr(self, "_row_load_warnings", [])
                if not warning.startswith("Kernels:")
            ]
            self._row_load_warnings.append(f"Kernels: {self._short_error(error)}")
        self._update_row_load_warnings()
        if args:
            self.apply_mode()

    def _load_kernel_rows(self, *args):
        """Build kernel rows. Wrapped so a bad kernel setting cannot abort the rest of the stage editor."""
        expanded = {row.kernel_name for row in getattr(self, "kernel_rows", []) if isinstance(row, StageKernelRow) and row.get_expanded()}
        expanded |= getattr(self, "_expand_kernel", set())
        self._expand_kernel = set()
        for row in getattr(self, "kernel_rows", []):
            self.kernels_pref_group.remove(row)
        self.kernel_rows = []
        if self.boot_kernel_argument is None:
            return # Target doesn't build kernels.
        names = stage_kernel_names(self.project_directory, self.stage)
        inherits = stage_inherits_kernels(self.stage)
        releng_names = releng_kernel_names(self.project_directory, self.stage)
        if inherits or releng_names:
            # When inherited, the Releng spec is the source of its kernels and settings.
            # Keep those names in the switch subtitle; only stage-specific additions get rows.
            inherit_row = Adw.SwitchRow(title="Use kernels of Releng template", active=inherits,
                                        subtitle=GLib.markup_escape_text(", ".join(releng_names) if releng_names else "Releng template has no kernels"))
            set_row_description(inherit_row, "On: use the Releng template's kernel list and settings as-is. Off: use a "
                                             "stage-specific kernel list, initially copied from Releng.")
            inherit_row.connect("notify::active", self._on_inherit_kernels_changed)
            inherit_row.changed_filter_key = self.boot_kernel_argument.attribute_name
            self._attach_restore_default_button(inherit_row, self.boot_kernel_argument.attribute_name)
            self._add_kernel_row(inherit_row)
        # Inherit-only kernels are summarized by the switch. Only stage-specific
        # additions get editable rows while inheritance remains enabled.
        editable_names = own_kernel_names(self.stage) if inherits else names
        for name in editable_names:
            row = StageKernelRow(project_directory=self.project_directory, stage=self.stage, kernel_name=name,
                                 on_rename=self._rename_kernel)
            row.set_expanded(name in expanded)
            set_row_description(row, "Kernel built for stage, expand it to see its settings.")
            remove_button = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove kernel", valign=Gtk.Align.CENTER)
            remove_button.add_css_class("flat")
            remove_button.connect("clicked", lambda _, name=name: self._remove_kernel(name))
            row.add_suffix(remove_button)
            if warning := live_cd_kernel_warning(self.project_directory, self.stage, name):
                icon = Gtk.Image(icon_name="danger-triangle-svgrepo-com-symbolic", tooltip_text=warning)
                icon.add_css_class("warning")
                row.add_suffix(icon)
            self._add_kernel_row(row)
        add_row = Adw.ButtonRow(title="Add kernel", start_icon_name="add-square-svgrepo-com-symbolic")
        add_row.is_kernel_add_action = True
        add_row.connect("activated", self._on_add_kernel)
        self._add_kernel_row(add_row)

    def _add_kernel_row(self, row):
        self.kernels_pref_group.add(row)
        self.kernel_rows.append(row)

    def _on_add_kernel(self, row):
        """New kernel with free name (renamed in its settings), its settings are shown. It's distribution kernel, of
        overlay used by stage when it has one, or of Gentoo."""
        inherits = stage_inherits_kernels(self.stage)
        names = own_kernel_names(self.stage) if inherits else stage_kernel_names(self.project_directory, self.stage)
        name = new_kernel_name(stage_kernel_names(self.project_directory, self.stage))
        self._expand_kernel = {name}
        set_kernel_names(self.project_directory, self.stage, names + [name], inherit=inherits)
        set_kernel_setting(self.project_directory, self.stage, name, "sources", default_kernel_package(self.project_directory, self.stage))
        set_kernel_setting(self.project_directory, self.stage, name, "distkernel", "yes")
        self.load_kernel_rows(True)

    def _rename_kernel(self, old_name: str, new_name: str) -> str | None:
        """Returns error when name can't be used."""
        if new_name == old_name:
            return None
        names = stage_kernel_names(self.project_directory, self.stage)
        if error := kernel_name_error(new_name, names):
            return error
        rename_kernel(self.project_directory, self.stage, old_name, new_name)
        self._expand_kernel = {new_name}
        GLib.idle_add(lambda: self.load_kernel_rows(True) and False) # After apply signal of row being replaced.
        return None

    def _on_inherit_kernels_changed(self, row, _param):
        inherit = row.get_active()
        if inherit == stage_inherits_kernels(self.stage):
            return
        # The switch controls only whether Releng's list is included. Preserve the
        # stage-specific kernels exactly as they are in either direction.
        set_kernel_names(self.project_directory, self.stage, own_kernel_names(self.stage), inherit=inherit)
        GLib.idle_add(lambda: self.load_kernel_rows(True) and False) # After signal of row being replaced.

    def _remove_kernel(self, name: str):
        remove_kernel(self.project_directory, self.stage, name)
        self.load_kernel_rows(True)

    def create_row_for_argument(self, argument: StageArgumentTargetDetails, toolset=None) -> Adw.PreferencesRow:
        if argument.details in CACHE_ARGUMENTS:
            return StageCacheRow(project_directory=self.project_directory, stage=self.stage, argument=argument)
        if argument.details == StageArgumentDetails.interpreter:
            return StageTextListRow(project_directory=self.project_directory, stage=self.stage, argument=argument,
                                    content_navigation_view=self.content_navigation_view)
        match argument.type:
            case StageArgumentType.select | StageArgumentType.multiselect | StageArgumentType.boolean:
                return StageOptionExpanderRow(
                    project_directory=self.project_directory,
                    stage=self.stage,
                    argument=argument,
                    is_item_selectable_handler=self.is_config_option_selectable,
                    toolset=toolset
                )
            case StageArgumentType.string_list | StageArgumentType.raw:
                return StageTextListRow(project_directory=self.project_directory, stage=self.stage, argument=argument,
                                        content_navigation_view=self.content_navigation_view)
            case _ if argument.details in SCRIPT_ARGUMENTS:
                return StageScriptRow(project_directory=self.project_directory, stage=self.stage, argument=argument)
            case _:
                # Stored automatic option is shown as unsupported value even if argument doesn't allow any.
                if (argument.details and argument.details.automatic_options) or isinstance(getattr(self.stage, argument.attribute_name, None), StageAutomaticOption):
                    return StageTextEntrySourceRow(project_directory=self.project_directory, stage=self.stage, argument=argument)
                return StageTextEntryRow(stage=self.stage, argument=argument)

    def can_change_argument(self, row, option: StageArgumentOption) -> bool:
        match option.argument:
            case (
                StageArgumentDetails.target
            ):
                # Changing a valid target would silently reinterpret its settings. If
                # the stored target is unsupported, allow choosing a valid replacement.
                return bool(row.selected_item and getattr(row.selected_item, "unsupported", False))
            case _:
                return True

    def pref_group_for_argument(self, argument: StageArgumentTargetDetails) -> Adw.PreferencesGroup | None:
        if not argument.details:
            return self.other_pref_group
        match argument.details:
            case (
                # Hidden, set by CatalystLab: name has own row, snapshot is selected for project and source_subpath is
                # generated from parent stage, as all stages of the tree are built by the app.
                StageArgumentDetails.name |
                StageArgumentDetails.snapshot_treeish |
                StageArgumentDetails.source_subpath
            ):
                return None
            case (
                StageArgumentDetails.parent |
                StageArgumentDetails.profile |
                StageArgumentDetails.target |
                StageArgumentDetails.releng_template
            ):
                return self.basic_config_pref_group
            case (
                StageArgumentDetails.subarch |
                StageArgumentDetails.asflags |
                StageArgumentDetails.cbuild |
                StageArgumentDetails.cflags |
                StageArgumentDetails.chost |
                StageArgumentDetails.common_flags |
                StageArgumentDetails.cxxflags |
                StageArgumentDetails.fcflags |
                StageArgumentDetails.fflags |
                StageArgumentDetails.ldflags |
                StageArgumentDetails.interpreter
            ):
                return self.architecture_pref_group
            case (
                StageArgumentDetails.rel_type |
                StageArgumentDetails.version_stamp |
                StageArgumentDetails.compression_mode
            ):
                return self.release_pref_group
            case (
                StageArgumentDetails.repos |
                StageArgumentDetails.keep_repos |
                StageArgumentDetails.binrepo_path |
                StageArgumentDetails.pkgcache_path |
                StageArgumentDetails.kerncache_path |
                StageArgumentDetails.portage_confdir
            ):
                return self.packages_pref_group
            case (StageArgumentDetails.stage4_packages | StageArgumentDetails.stage4_unmerge |
                  StageArgumentDetails.livecd_packages | StageArgumentDetails.livecd_unmerge):
                return self.packages_pref_group
            case (StageArgumentDetails.livecd_bootargs | StageArgumentDetails.livecd_cdtar |
                  StageArgumentDetails.livecd_fstype | StageArgumentDetails.livecd_iso |
                  StageArgumentDetails.livecd_motd | StageArgumentDetails.livecd_overlay |
                  StageArgumentDetails.livecd_readme | StageArgumentDetails.livecd_type |
                  StageArgumentDetails.livecd_iso_extra_partition |
                  StageArgumentDetails.livecd_verify | StageArgumentDetails.livecd_volid):
                return self.live_image_pref_group
            case (StageArgumentDetails.livecd_use | StageArgumentDetails.livecd_depclean |
                  StageArgumentDetails.livecd_empty | StageArgumentDetails.livecd_fsops |
                  StageArgumentDetails.livecd_fsscript | StageArgumentDetails.livecd_gk_mainargs |
                  StageArgumentDetails.livecd_linuxrc | StageArgumentDetails.livecd_modblacklist |
                  StageArgumentDetails.livecd_rcadd | StageArgumentDetails.livecd_rcdel |
                  StageArgumentDetails.livecd_rm | StageArgumentDetails.livecd_root_overlay |
                  StageArgumentDetails.livecd_users):
                return self.live_system_pref_group
            case (StageArgumentDetails.stage4_use | StageArgumentDetails.stage4_empty |
                  StageArgumentDetails.stage4_fsscript | StageArgumentDetails.stage4_gk_mainargs |
                  StageArgumentDetails.stage4_groups | StageArgumentDetails.stage4_linuxrc |
                  StageArgumentDetails.stage4_rcadd | StageArgumentDetails.stage4_rcdel |
                  StageArgumentDetails.stage4_rm | StageArgumentDetails.stage4_root_overlay |
                  StageArgumentDetails.stage4_ssh_public_keys | StageArgumentDetails.stage4_users):
                return self.stage_setup_pref_group
            case (StageArgumentDetails.catalyst_use | StageArgumentDetails.decompressor_search_order |
                  StageArgumentDetails.hostuse | StageArgumentDetails.install_mask |
                  StageArgumentDetails.portage_prefix |
                  StageArgumentDetails.update_seed | StageArgumentDetails.update_seed_command):
                return self.build_behavior_pref_group
            case StageArgumentDetails.rename_regexp:
                return self.other_pref_group
            case _:
                return self.build_behavior_pref_group

    def is_config_option_selectable(self, row, option):
        return self.can_change_argument(row=row, option=option)

    def argument_changed(self, row):
        if (row.argument.type in (StageArgumentType.raw_single_line, StageArgumentType.string_list, StageArgumentType.raw)
                or isinstance(row, StageTextSourceRow)):
            ProjectManager.shared().change_stage_argument(
                project=self.project_directory,
                stage=self.stage,
                argument=row.argument.details or row.argument.name,
                value=row.value
            )
            # Reloading option rows would discard unapplied edits of text rows, refresh only resolved values.
            for r in self.configuration_rows:
                if r.argument != row.argument and isinstance(r, (StageTextSourceRow, StageCacheRow)):
                    r.refresh_options()
            self._refresh_changed_filter()
            return
        if row.argument.details.type == StageArgumentType.select:
            ProjectManager.shared().change_stage_argument(
                project=self.project_directory,
                stage=self.stage,
                argument=row.argument.details,
                value=row.selected_item.value if row.selected_item else None
            )
            if row.argument.details == StageArgumentDetails.target:
                # Target determines which Catalyst arguments and editors are valid.
                self._refresh_changed_filter()
                self.load_configuration_rows()
                return
        if row.argument.details.type == StageArgumentType.multiselect:
            ProjectManager.shared().change_stage_argument(
                project=self.project_directory,
                stage=self.stage,
                argument=row.argument.details,
                value=[item.value for item in row.selected_items] if row.selected_items else None
            )
        if row.argument.details.type == StageArgumentType.boolean:
            ProjectManager.shared().change_stage_argument(
                project=self.project_directory,
                stage=self.stage,
                argument=row.argument.details,
                value=row.selected_item.value if row.selected_item is not None else None
            )
        # Reload other option rows (text rows keep their own, possibly unapplied, content)
        for r in self.configuration_rows:
            if r.argument != row.argument and isinstance(r, StageOptionExpanderRow):
                r.load_state()
            elif r.argument != row.argument and isinstance(r, (StageTextSourceRow, StageCacheRow)):
                r.refresh_options() # Keeps possibly unapplied text, only updates availability of automatic options.
        self._refresh_changed_filter()

    def _refresh_changed_filter(self):
        from catalystlab import project_stage_overrides
        self.stage_override_metadata = project_stage_overrides.get(
            self.project_directory.directory_path(), self.stage.id
        )
        self.apply_mode()
    def _on_unrealize(self, widget):
        if getattr(self, "_window_active_handler", None) and (root := self.get_root()):
            root.disconnect(self._window_active_handler)
            self._window_active_handler = None

    def _on_window_active_changed(self, window, param):
        # Overlay files could be edited outside of app, refresh their counts.
        if window.is_active():
            for row in getattr(self, "configuration_rows", []):
                if isinstance(row, StageOptionExpanderRow) and row.uses_stage_overlay_folder:
                    row.load_state()

    # Monitoring stage changes
    # --------------------------------------------------------------------------

    def monitor_information_changes(self):
        """React to changes in UI and store them."""
        subscriptions = [
            (self.stage.event_bus, ProjectStageEvent.NAME_CHANGED, self.on_name_changed)
        ]
        for bus, event, handler in subscriptions:
            bus.subscribe(event, handler)

    def on_name_changed(self, data):
        self._page.set_title(self.stage.name)

    # Handle UI
    # --------------------------------------------------------------------------

    @Gtk.Template.Callback()
    def on_stage_name_activate(self, sender):
        new_name = self.stage_name_row.get_text()
        if new_name == self.stage.name:
            self.get_root().set_focus(None)
            return
        is_name_available = ProjectManager.shared().is_stage_name_available(
            project=self.project_directory,
            name=self.stage_name_row.get_text()
        ) or self.stage_name_row.get_text() == self.stage.name
        try:
            if not is_name_available:
                raise RuntimeError(f"Stage name {new_name} is not available")
            ProjectManager.shared().rename_stage(project=self.project_directory, stage=self.stage, name=new_name)
            self.get_root().set_focus(None)
            self.load_stage_details()
            self._refresh_changed_filter()
        except Exception as e:
            print(f"Error renaming stage: {e}")
            self.stage_name_row.add_css_class("error")
            self.stage_name_row.grab_focus()

    @Gtk.Template.Callback()
    def on_delete_clicked(self, sender):
        detached_stages = ProjectManager.shared().stages_using_parent(project=self.project_directory, stage=self.stage)
        body = f"Stage \"{self.stage.name}\" will be removed from the project. This can't be undone."
        if detached_stages:
            names = ", ".join(f"\"{stage.name}\"" for stage in detached_stages)
            body += f"\n\nIt is used as parent by {names}. Their parent will be cleared, and values they inherit from it will need to be set again."
        dialog = Adw.AlertDialog(heading="Delete stage?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self.on_delete_response)
        dialog.present(self.get_root())

    def on_delete_response(self, dialog, response: str):
        if response != "delete":
            return
        try:
            ProjectManager.shared().remove_stage(project=self.project_directory, stage=self.stage)
        except Exception as e:
            print(f"Error deleting stage: {e}")
            return
        if self.content_navigation_view:
            self.content_navigation_view.pop()

    @Gtk.Template.Callback()
    def on_stage_name_changed(self, sender):
        is_name_available = ProjectManager.shared().is_stage_name_available(
            project=self.project_directory,
            name=self.stage_name_row.get_text()
        ) or self.stage_name_row.get_text() == self.stage.name
        self._name_is_used = not is_name_available
        self.name_used_row.set_visible(self._name_is_used and self.mode_toggle.get_active() != 2)
        self.stage_name_row.remove_css_class("error")

    # Helper functions
    # --------------------------------------------------------------------------

def create_unsupported_options(missing_values: list, argument: StageArgumentDetails | None) -> list[StageArgumentOption]:
    """Creates dummy entries for options that are currently set but not available in available options."""
    if not missing_values:
        return []
    return [
        StageArgumentOption(
            raw=value,
            display="Unsupported value",
            subtitle=value if isinstance(value, str) or isinstance(value, uuid.UUID) else value.name if isinstance(value, StageAutomaticOption) else "(unknown)",
            value=value,
            argument=argument,
            unsupported=True
        )
        for value in missing_values
    ]

def include_unsupported_options(options: list[StageArgumentOption], values: list,
                                argument: StageArgumentDetails | None) -> list[StageArgumentOption]:
    supported = {option.value for option in options}
    missing = [value for value in values if value is not None and value not in supported]
    return create_unsupported_options(missing, argument) + options

class StageOptionExpanderRow(ItemSelectionExpanderRow):

    def __init__(self, project_directory: ProjectDirectory, stage: ProjectStage, argument: StageArgumentTargetDetails, is_item_selectable_handler, toolset=None):
        super().__init__()
        self.title = argument.display_name
        self.item_title_property_name = 'display'
        self.item_subtitle_property_name = 'subtitle'
        self.item_unsupported_property_name = 'unsupported'
        self.argument = argument
        self.project_directory = project_directory
        self.stage = stage
        self.toolset = toolset
        self.display_none = not argument.required
        self.allow_multiselect = argument.details.type == StageArgumentType.multiselect if argument.details else False
        self.connect("is-item-selectable", is_item_selectable_handler)
        self.unsupported_value_row = Adw.ActionRow(
            title="Unsupported value",
            subtitle="This saved value is not available for the current target or toolset. Select an available option below to replace it."
        )
        warning_icon = Gtk.Image.new_from_icon_name("danger-triangle-svgrepo-com-symbolic")
        warning_icon.add_css_class("warning")
        self.unsupported_value_row.add_prefix(warning_icon)
        self.unsupported_value_row.set_visible(False)
        self.unsupported_selection_icon = Gtk.Image.new_from_icon_name("danger-triangle-svgrepo-com-symbolic")
        self.unsupported_selection_icon.add_css_class("warning")
        self.unsupported_selection_icon.set_visible(False)
        self.add_suffix(self.unsupported_selection_icon)
        # Insert before the selectable values so the reason and recovery path are
        # immediately visible when this row is expanded.
        self.add_row(self.unsupported_value_row)
        self.load_state()

    def load_state(self):
        details = self.argument.details
        if not details:
            return
        current_value = getattr(self.stage, details.name, None) # Mapped to object
        if details.type == StageArgumentType.multiselect:
            options = self._options()
            current_values = current_value
            # Stage can store None (all items deselected) or single value, show it as list.
            if current_values is None:
                current_values = []
            elif not isinstance(current_values, list):
                current_values = [current_values]
            if self.argument.details in FILE_OVERLAY_ARGUMENTS:
                # Values stored by older versions (inherit options, path of stage folder) are shown as sources.
                current_values = root_overlay_values(self.project_directory, self.stage, self.argument.details)
            options = visible_stage_argument_options(options, current_values)
            options = include_unsupported_options(options, current_values, self.argument.details)
            self.selected_items = [option for option in options if option.value in current_values] if current_values else []
        elif details.type in (StageArgumentType.select, StageArgumentType.boolean):
            options = self._options(boolean=details.type == StageArgumentType.boolean)
            options = visible_stage_argument_options(options, [current_value])
            options = include_unsupported_options(options, [current_value], self.argument.details)
            self.selected_item = next((item for item in options if item.value == current_value), None)
        else:
            return
        self.set_static_list(options)

    def _options(self, boolean: bool = False) -> list[StageArgumentOption]:
        automatic = load_catalyst_stage_automatic_arguments_options(
            stage=self.stage, arg_details=self.argument, project_directory=self.project_directory
        )
        available = (load_catalyst_stage_arguments_options_for_boolean(arg_details=self.argument) if boolean else
                     load_catalyst_stage_arguments_options(
                         project_directory=self.project_directory, stage=self.stage, arg_details=self.argument,
                         toolset=self.toolset
                     ) or [])
        return automatic + available

    def set_static_list(self, items: list):
        # Show values automatic options resolve to (inherit from parent, releng template...) when they can be determined.
        self.resolved_values: dict[StageAutomaticOption, str] = {}
        for option in items:
            if isinstance(option.value, StageAutomaticOption) and not option.unsupported:
                if resolved := resolved_stage_argument_display(self.project_directory, self.stage, self.argument.name, option.value):
                    self.resolved_values[option.value] = resolved
                    option.subtitle = GLib.markup_escape_text(resolved)
        super().set_static_list(items)
        if self.uses_stage_overlay_folder:
            self._add_open_stage_overlay_button()

    @property
    def uses_stage_overlay_folder(self) -> bool:
        """Argument combined from sources that include folder in stage directory (portage_confdir, root overlay)."""
        return self.argument.details == StageArgumentDetails.portage_confdir or self.argument.details in FILE_OVERLAY_ARGUMENTS

    def _add_open_stage_overlay_button(self):
        row = next((row for row in getattr(self, "rows", []) if row.item.value == StagePortageConfdirSource.STAGE_OVERLAY), None)
        if row is None:
            return
        button = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Open stage overlay folder", valign=Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.connect("clicked", self._on_open_stage_overlay_clicked)
        row.add_suffix(button)

    def _on_open_stage_overlay_clicked(self, button):
        # Folder is created when needed and kept when overlay is disabled, to allow enabling it back.
        if self.argument.details in FILE_OVERLAY_ARGUMENTS:
            path = stage_root_overlay_path(self.project_directory, self.stage, self.argument.details)
        else:
            path = stage_overlay_path(self.project_directory, self.stage)
        os.makedirs(path, exist_ok=True)
        Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(self.get_root(), None, None)

    def display_selected_item(self):
        super().display_selected_item()
        selected = (self.selected_items or []) if self.allow_multiselect else ([self.selected_item] if self.selected_item else [])
        has_unsupported = any(getattr(item, "unsupported", False) for item in selected)
        self.unsupported_value_row.set_visible(has_unsupported)
        self.unsupported_selection_icon.set_visible(has_unsupported)
        if any(isinstance(item.value, StageAutomaticOption) for item in selected):
            self.set_subtitle(", ".join(GLib.markup_escape_text(self._item_display(item)) for item in selected))

    def _item_display(self, item: StageArgumentOption) -> str:
        resolved = getattr(self, "resolved_values", {}).get(item.value) if isinstance(item.value, StageAutomaticOption) else None
        return f"{item.display}: {resolved}" if resolved else item.display


class StageTextEntryRow(Adw.EntryRow):
    """Edits single line text arguments. Value is stored as str, or None when empty."""

    def __init__(self, stage: ProjectStage, argument: StageArgumentTargetDetails):
        super().__init__()
        self.stage = stage
        self.argument = argument
        self.value = None
        self.event_bus = EventBus[ItemSelectionViewEvent]()
        self.set_title(argument.display_name)
        self.set_show_apply_button(True)
        self.warning_icon = Gtk.Image.new_from_icon_name("danger-triangle-svgrepo-com-symbolic")
        self.warning_icon.add_css_class("warning")
        self.warning_icon.set_tooltip_text("This value is required")
        self.add_suffix(self.warning_icon)
        self.connect("apply", self.on_apply)
        self.load_state()

    def load_state(self):
        current_value = getattr(self.stage, self.argument.attribute_name, None)
        if isinstance(current_value, list):
            current_value = " ".join(str(item) for item in current_value)
        self.value = str(current_value) if current_value else None
        self.set_text(self.value or "")
        self.update_warning()

    def on_apply(self, sender):
        self.value = self.get_text().strip() or None
        # Only rewrite when trimming changed something, as set_text makes AdwEntryRow show apply button again.
        if self.get_text() != (self.value or ""):
            self.set_text(self.value or "")
        self.update_warning()
        self.event_bus.emit(ItemSelectionViewEvent.ITEM_CHANGED, self)
        if root := self.get_root():
            root.set_focus(None)

    def update_warning(self):
        self.warning_icon.set_visible(self.argument.required and not self.value)

class StageTextSourceRow(Adw.ExpanderRow):
    """Base for text arguments edited inside expander row. If argument allows automatic options (inherit from parent,
    releng template...), they can be selected instead of custom value edited by subclass editor.
    Value is StageAutomaticOption, custom value, or None when empty."""

    def __init__(self, project_directory: ProjectDirectory, stage: ProjectStage, argument: StageArgumentTargetDetails):
        super().__init__()
        self.project_directory = project_directory
        self.stage = stage
        self.argument = argument
        self.uses_package_picker = argument.details in (
            StageArgumentDetails.stage4_packages, StageArgumentDetails.livecd_packages
        )
        self.value = None
        self.resolved_values: dict[StageAutomaticOption, str] = {}
        self._loading = False
        self.event_bus = EventBus[ItemSelectionViewEvent]()
        self.set_title(argument.display_name)
        self.warning_icon = Gtk.Image.new_from_icon_name("danger-triangle-svgrepo-com-symbolic")
        self.warning_icon.add_css_class("warning")
        self.add_suffix(self.warning_icon)
        self._setup_sources()
        self.editor_row = self._create_editor_row()
        self.add_row(self.editor_row)
        self.load_state()

    # Sources:

    def _setup_sources(self):
        """Adds rows to select automatic option or custom value. Without automatic options only custom value is used.
        Arguments joining inherited values with own value (allows_additions) have check boxes, so inherit options and
        custom value can be selected together, others have radio buttons."""
        self.automatic_options: list[StageArgumentOption] = load_catalyst_stage_automatic_arguments_options(stage=self.stage, arg_details=self.argument, project_directory=self.project_directory) or []
        # Add entries for unsupported values (automatic option stored, but not allowed for this argument)
        current_value = getattr(self.stage, self.argument.attribute_name, None)
        stored_options = current_value if isinstance(current_value, list) else [current_value]
        allowed = {option.value for option in self.automatic_options}
        missing_values = list(dict.fromkeys(item for item in stored_options if isinstance(item, StageAutomaticOption) and item not in allowed))
        self.unsupported_options = create_unsupported_options(missing_values=missing_values, argument=self.argument.details)
        self.source_rows: dict[StageAutomaticOption | None, Adw.ActionRow] = {}
        self.source_check_buttons: dict[StageAutomaticOption | None, Gtk.CheckButton] = {}
        self._custom_enabled = True
        if not self.automatic_options and not self.unsupported_options:
            return
        group: Gtk.CheckButton | None = None
        options = self.unsupported_options + self.automatic_options
        if not self.uses_package_picker:
            options.append(None) # None stands for custom value.
        for option in options:
            # Same rows as in option lists (eg. Profile), so unavailable options look the same.
            row = ItemRow(
                item=option,
                item_title_property_name='display',
                item_subtitle_property_name='subtitle',
                item_status_property_name=None,
                item_unsupported_property_name='unsupported',
                item_icon=None
            ) if option else Adw.ActionRow(title="Custom value")
            check_button = Gtk.CheckButton()
            if not self.allows_additions:
                if group:
                    check_button.set_group(group)
                else:
                    group = check_button
            check_button.connect("toggled", self._on_source_toggled, option.value if option else None)
            row.add_prefix(check_button)
            row.set_activatable_widget(check_button)
            self.add_row(row)
            self.source_rows[option.value if option else None] = row
            self.source_check_buttons[option.value if option else None] = check_button
        if self.allows_additions and None in self.source_rows:
            self.source_rows[None].set_subtitle("Joined with selected inherited values")
        self._update_source_subtitles()

    def refresh_options(self):
        """Updates availability of automatic options, which depends on other arguments (parent, releng template)."""
        if not self.source_rows:
            return
        self.automatic_options = load_catalyst_stage_automatic_arguments_options(stage=self.stage, arg_details=self.argument, project_directory=self.project_directory) or []
        self._update_source_subtitles()
        self.update_display()

    def _update_source_subtitles(self):
        """Shows values automatic options resolve to (inherit from parent, releng template...) when they can be determined."""
        self.resolved_values = {}
        for option in self.automatic_options:
            resolved = None if option.unsupported else resolved_stage_argument_display(self.project_directory, self.stage, self.argument.name, option.value)
            if resolved:
                self.resolved_values[option.value] = resolved
            row = self.source_rows[option.value]
            row.set_subtitle(GLib.markup_escape_text(resolved or option.subtitle or ""))
            # Availability can change after creating row (parent or releng template changed).
            if option.unsupported:
                row.add_css_class('warning')
            else:
                row.remove_css_class('warning')

    def _update_source_visibility(self):
        selected = set(self.inherit_options())
        options = self.unsupported_options + self.automatic_options
        visible = {option.value for option in visible_stage_argument_options(options, selected)}
        for value, row in self.source_rows.items():
            row.set_visible(value is None or value in visible)

    def _on_source_toggled(self, button: Gtk.CheckButton, source: StageAutomaticOption | None):
        if self._loading:
            return
        if self.allows_additions:
            # Check boxes: value joins all selected inherit options and custom value.
            if source is None:
                self._custom_enabled = button.get_active()
            options = [option for option, check_button in self.source_check_buttons.items()
                       if option is not None and check_button.get_active()]
            self.value = self._joined_value(options, self.get_editor_value() if self._custom_enabled else None)
        else:
            if not button.get_active():
                return
            self.value = source if source is not None else self.get_editor_value()
        self.update_display()
        self.event_bus.emit(ItemSelectionViewEvent.ITEM_CHANGED, self)

    @property
    def allows_additions(self) -> bool:
        """Inherited values can be joined with custom value (stored as inherit options followed by own entries)."""
        return bool(self.argument.details and self.argument.details.allows_additions)

    def inherit_options(self) -> list[StageAutomaticOption]:
        """Selected automatic options."""
        if isinstance(self.value, StageAutomaticOption):
            return [self.value]
        if self.allows_additions and isinstance(self.value, list):
            if (joined := split_additions(self.value)) is not None:
                return joined[0]
            if len(self.value) == 1 and isinstance(self.value[0], StageAutomaticOption):
                return [self.value[0]]
        return []

    def own_value(self):
        """Custom value (joined with inherited values when they are selected too), None when it isn't used."""
        if not self.is_custom():
            return None
        if self.allows_additions and (joined := split_additions(self.value)) is not None:
            return self.normalized_custom_value(joined[1])
        return None if self.inherit_options() else self.value

    def _joined_value(self, options: list[StageAutomaticOption], own):
        own_items = (own if isinstance(own, list) else [own]) if own else []
        if not options:
            return own
        if not own_items and len(options) == 1:
            return options[0]
        return [*options, *own_items]

    def is_custom(self) -> bool:
        """Custom value is used (alone, or joined with inherited values)."""
        if self.uses_package_picker:
            return True
        if self.allows_additions and self.source_rows:
            return self._custom_enabled
        return not self.inherit_options()

    def own_entries_not_inherited(self, own):
        """Own entries that aren't already supplied verbatim by selected sources.

        String-list arguments contain multiple entries, while additions to scalar text
        arguments (eg. BOOT args) are a single string. Keep the latter intact instead
        of iterating over its characters.
        """
        if not own or not self.allows_additions:
            return own or []
        inherited = set()
        for option in self.inherit_options():
            inherited.update(map(str, _as_list(resolve_stage_argument(
                self.project_directory, self.stage, self.argument.name, option=option
            ))))
        if self.argument.details and self.argument.details.type == StageArgumentType.raw_single_line:
            return [] if str(own) in inherited else own
        own_items = _as_list(own)
        return [item for item in own_items if str(item) not in inherited]

    # State:

    def load_state(self):
        current_value = getattr(self.stage, self.argument.attribute_name, None)
        if (self.argument.details == StageArgumentDetails.interpreter and current_value is None
                and self.argument.attribute_name not in getattr(self.stage, "_explicit_none_arguments", set())):
            # Show Automatic as the implicit default without writing it into stage.json.
            current_value = StageAutomaticOption.GENERATE_AUTOMATICALLY
        self._loading = True
        joined = split_additions(current_value) if self.allows_additions else None
        if isinstance(current_value, StageAutomaticOption) or (isinstance(current_value, list) and current_value
                                                                and all(isinstance(item, StageAutomaticOption) for item in current_value)):
            self.value = current_value[0] if isinstance(current_value, list) and len(current_value) == 1 else current_value
            self._custom_enabled = False
            self.set_editor_value(None)
        elif joined is not None:
            own = self.normalized_custom_value(joined[1])
            self.value = self._joined_value(joined[0], own)
            self._custom_enabled = True
            self.set_editor_value(own)
        else:
            self.value = self.normalized_custom_value(current_value)
            # Empty value with check boxes: nothing is selected.
            self._custom_enabled = not (self.allows_additions and self.source_rows and self.value is None)
            self.set_editor_value(self.value)
        if self.uses_package_picker:
            self._custom_enabled = True
        options = self.inherit_options()
        for option, check_button in self.source_check_buttons.items():
            check_button.set_active(self._custom_enabled if option is None else option in options)
        self._loading = False
        self.update_display()

    def apply_custom_value(self):
        """Call from editor when custom value is applied."""
        value = self.get_editor_value()
        self.value = self._joined_value(self.inherit_options(), value) if self.allows_additions else value
        self.set_editor_value(value)
        self.update_display()
        self.event_bus.emit(ItemSelectionViewEvent.ITEM_CHANGED, self)

    def update_display(self):
        self._update_source_visibility()
        self.editor_row.set_visible(self.is_custom())
        options = self.inherit_options()
        known = {option.value: option for option in self.unsupported_options + self.automatic_options}
        parts, unavailable = [], False
        for value in options:
            option = known.get(value)
            if option is None or option.unsupported:
                unavailable = True
            display = option.display if option else f"Unsupported value: {value.name}"
            resolved = self.resolved_values.get(value)
            parts.append(f"{display}: {resolved}" if resolved and len(options) == 1 and not self.own_value() else display)
        own = self.own_value()
        own_additions = self.own_entries_not_inherited(own)
        if self.uses_package_picker:
            parts = [known[value].display for value in options if value in known]
            if own_additions:
                parts.append(f"{len(own_additions)} additional package{'s' if len(own_additions) != 1 else ''}")
            subtitle = " + ".join(parts) or "Select packages"
            unavailable = any(known.get(value) is None or known[value].unsupported for value in options)
            self.set_subtitle(GLib.markup_escape_text(subtitle))
            self.warning_icon.set_visible(unavailable or self.argument.required and not parts)
            self.warning_icon.set_tooltip_text("Selected option is not available for this stage" if unavailable else "This value is required")
            if hasattr(self, "package_action_row"):
                sorted_additions = sorted(own_additions, key=str.casefold)
                own_summary = ", ".join(sorted_additions[:3]) if sorted_additions else (
                    "Already included through inheritance" if own else "Choose packages and versions"
                )
                if len(sorted_additions) > 3:
                    own_summary += f" (+{len(sorted_additions) - 3} more)"
                self.package_action_row.set_subtitle(own_summary)
            self.project_directory.event_bus.emit(GitDirectoryEvent.CONTENT_CHANGED, self.project_directory)
            return
        if own:
            if own_additions:
                parts.append(f"own: {self.custom_value_display(own_additions)}" if options else self.custom_value_display(own_additions))
        subtitle = " + ".join(parts) or "(None)"
        if unavailable:
            show_warning, warning = True, "Selected option is not available for this stage"
        else:
            show_warning, warning = self.argument.required and not options and not own, "This value is required"
        self.set_subtitle(GLib.markup_escape_text(subtitle))
        self.warning_icon.set_visible(show_warning)
        self.warning_icon.set_tooltip_text(warning)

    # Subclass editor interface:

    def _create_editor_row(self) -> Gtk.Widget:
        raise NotImplementedError

    def get_editor_value(self):
        """Returns custom value currently entered in editor, normalized."""
        raise NotImplementedError

    def set_editor_value(self, value):
        raise NotImplementedError

    def normalized_custom_value(self, value):
        """Converts stored value to custom value format used by editor."""
        raise NotImplementedError

    def custom_value_display(self, value) -> str:
        raise NotImplementedError

class StageTextEntrySourceRow(StageTextSourceRow):
    """Edits single line text arguments that also allow automatic options. Custom value is stored as str."""

    def _create_editor_row(self) -> Gtk.ListBoxRow:
        self.entry_row = Adw.EntryRow(title="Value")
        self.entry_row.set_show_apply_button(True)
        self.entry_row.connect("apply", self.on_apply)
        return self.entry_row

    def on_apply(self, sender):
        self.apply_custom_value()
        if root := self.get_root():
            root.set_focus(None)

    def get_editor_value(self):
        return self.entry_row.get_text().strip() or None

    def set_editor_value(self, value):
        # Only rewrite when text differs, as set_text makes AdwEntryRow show apply button again.
        if self.entry_row.get_text() != (value or ""):
            self.entry_row.set_text(value or "")

    def normalized_custom_value(self, value):
        if isinstance(value, list):
            value = " ".join(str(item) for item in value)
        return str(value) if value else None

    def custom_value_display(self, value) -> str:
        return value

# Arguments with path of script run by catalyst in stage (fsscript). Scripts in stage folder are edited in the app.
SCRIPT_ARGUMENTS = (StageArgumentDetails.stage4_fsscript, StageArgumentDetails.livecd_fsscript)
_STAGE_DIR = "@STAGE_DIR@/"

class StageScriptRow(StageTextEntrySourceRow):
    """Path of script catalyst copies into stage and runs (fsscript). Script in stage folder (path @STAGE_DIR@/...) is
    written in the app: its text is saved to the file, which is part of the project, and @STAGE_DIR@ is replaced with the
    folder when building. Without script, one can be written in stage folder."""

    _NEW_SCRIPT = "#!/bin/bash\n# Runs in stage after its packages are installed.\nset -e\n\n"

    def __init__(self, project_directory: ProjectDirectory, stage: ProjectStage, argument: StageArgumentTargetDetails):
        self.project_directory = project_directory
        self.stage = stage
        self._script_rows_created = False
        super().__init__(project_directory=project_directory, stage=stage, argument=argument)
        self._create_script_rows()
        self.update_display()

    def _create_script_rows(self):
        self.text_view = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=True,
                                      top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        self.text_view.set_size_request(-1, 240)
        self.text_view.get_buffer().connect("changed", lambda buffer: self._update_script_state())
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.text_view.add_controller(key_controller)
        self.script_label = Gtk.Label(halign=Gtk.Align.START, hexpand=True, ellipsize=Pango.EllipsizeMode.START)
        self.script_label.add_css_class("dimmed")
        self.script_label.add_css_class("caption")
        self.revert_button = Gtk.Button(label="Revert")
        self.revert_button.connect("clicked", lambda _: self._load_script())
        self.save_button = Gtk.Button(label="Save")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", lambda _: self._save_script())
        buttons = Gtk.Box(spacing=6)
        for widget in (self.script_label, self.revert_button, self.save_button):
            buttons.append(widget)
        editor = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        editor.append(Gtk.Frame(child=Gtk.ScrolledWindow(child=self.text_view, min_content_height=240, max_content_height=480,
                                                          propagate_natural_height=True, hscrollbar_policy=Gtk.PolicyType.NEVER)))
        editor.append(buttons)
        self.script_row = Gtk.ListBoxRow(activatable=False, selectable=False, child=editor)
        self.add_row(self.script_row)
        self.write_row = Adw.ButtonRow(title="Write script in stage folder", start_icon_name="document-edit-symbolic")
        self.write_row.connect("activated", lambda _: self._write_new_script())
        self.add_row(self.write_row)
        self.outside_row = Adw.ActionRow(title="Script is outside of project", subtitle="Only scripts in stage folder (@STAGE_DIR@/...) are edited in the app")
        self.add_row(self.outside_row)
        self._script_rows_created = True
        self._loaded_path = None

    def _script_file(self, path) -> str | None:
        """File of script in stage folder, None for other paths."""
        if not isinstance(path, str) or not path.startswith(_STAGE_DIR):
            return None
        relative = os.path.normpath(path[len(_STAGE_DIR):])
        if relative.startswith("..") or os.path.isabs(relative):
            return None
        return os.path.join(self.project_directory.stage_directory_path(self.stage.name), relative)

    def update_display(self):
        super().update_display()
        if not getattr(self, "_script_rows_created", False):
            return
        custom = self.is_custom()
        path = self.value if custom else None
        file = self._script_file(path)
        self.script_row.set_visible(custom and file is not None)
        self.write_row.set_visible(custom and not path)
        self.outside_row.set_visible(custom and bool(path) and file is None)
        if file is not None and path != self._loaded_path:
            self._load_script()

    def _load_script(self):
        file = self._script_file(self.value if self.is_custom() else None)
        try:
            with open(file, encoding="utf-8") as script:
                text = script.read()
        except (OSError, TypeError):
            text = ""
        self._loaded_path = self.value
        self._saved_text = text
        self.text_view.get_buffer().set_text(text)
        self._update_script_state()

    def _text(self) -> str:
        buffer = self.text_view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def _update_script_state(self):
        changed = self._text() != getattr(self, "_saved_text", None)
        self.save_button.set_sensitive(changed)
        self.revert_button.set_sensitive(changed)
        file = self._script_file(self.value if self.is_custom() else None)
        name = os.path.relpath(file, self.project_directory.directory_path()) if file else ""
        self.script_label.set_label(f"{name} · not saved" if changed else name)

    def _save_script(self):
        file = self._script_file(self.value if self.is_custom() else None)
        if file is None:
            return
        text = self._text()
        os.makedirs(os.path.dirname(file), exist_ok=True)
        with open(file, "w", encoding="utf-8") as script:
            script.write(text)
        self._saved_text = text
        self._update_script_state()
        # Script is file of project, its Git status changes.
        self.project_directory.event_bus.emit(GitDirectoryEvent.CONTENT_CHANGED, self.project_directory)

    def _write_new_script(self):
        """New script in stage folder, its path is set as value."""
        name = "fsscript.sh"
        number = 2
        folder = self.project_directory.stage_directory_path(self.stage.name)
        while os.path.exists(os.path.join(folder, name)):
            name, number = f"fsscript-{number}.sh", number + 1
        self.set_editor_value(_STAGE_DIR + name)
        self.apply_custom_value()
        self.text_view.get_buffer().set_text(self._NEW_SCRIPT)
        self._save_script()
        GLib.idle_add(lambda: self.text_view.grab_focus() and False)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_s and state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.META_MASK):
            self._save_script()
            return True
        return False

class StageTextListRow(StageTextSourceRow):
    """Edits list arguments (packages, use, rcadd...) one entry per line. Custom value is stored as list[str]."""

    SUBTITLE_MAX_ITEMS = 3

    def __init__(self, project_directory: ProjectDirectory, stage: ProjectStage,
                 argument: StageArgumentTargetDetails, content_navigation_view=None):
        self.content_navigation_view = content_navigation_view
        self.own_packages = []
        super().__init__(project_directory, stage, argument)

    def _create_editor_row(self) -> Gtk.Widget:
        if self.uses_package_picker:
            self.package_action_row = Adw.ActionRow(
                title="Additional packages", subtitle="Choose packages and versions", activatable=True
            )
            arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
            self.package_action_row.add_suffix(arrow)
            self.package_action_row.connect("activated", self._open_package_picker)
            return self.package_action_row
        self.text_view = Gtk.TextView(
            monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False,
            top_margin=8, bottom_margin=8, left_margin=8, right_margin=8
        )
        self.text_view.set_size_request(-1, 96)
        self.text_view.get_buffer().connect("changed", self.on_buffer_changed)
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self.on_key_pressed)
        self.text_view.add_controller(key_controller)
        frame = Gtk.Frame(child=self.text_view)

        self.hint_label = Gtk.Label(
            label="One entry per line. Ctrl+Enter to apply.", wrap=True, xalign=0,
            halign=Gtk.Align.START, hexpand=True, css_classes=["dimmed", "caption"]
        )
        self.revert_button = Gtk.Button(label="Revert", valign=Gtk.Align.CENTER)
        self.revert_button.connect("clicked", lambda button: self.set_editor_value(self.own_value()))
        self.apply_button = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", lambda button: self.apply_custom_value())
        buttons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for widget in (self.hint_label, self.revert_button, self.apply_button):
            buttons_box.append(widget)

        editor_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                             margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        editor_box.append(frame)
        editor_box.append(buttons_box)
        return Gtk.ListBoxRow(activatable=False, selectable=False, child=editor_box)

    def _open_package_picker(self, *_):
        if not self.content_navigation_view:
            return
        view = StagePackagePickerView(
            self.project_directory, self.stage, self.argument, self.own_value() or [],
            self._apply_picker_value, self.inherit_options()
        )
        view.content_navigation_view = self.content_navigation_view
        self.content_navigation_view.push_view(view, title="Browse packages")

    def _apply_picker_value(self, value, options=None):
        if not self.uses_package_picker:
            self.set_editor_value(value)
            self.apply_custom_value()
            return
        self._custom_enabled = True
        self.set_editor_value(value)
        self.value = self._joined_value(options, value) if self.allows_additions else value
        self._loading = True
        selected = set(options or [])
        for option, check in self.source_check_buttons.items():
            check.set_active(option in selected)
        self._loading = False
        self.update_display()
        # The picker applies its changes immediately before popping back to this row. Persist
        # synchronously here so the stage value (and the previous view's source toggles) is
        # already up to date when navigation returns; an idle-scheduled event can run after
        # the pushed view transition and leave the previous row showing stale state.
        ProjectManager.shared().change_stage_argument(
            project=self.project_directory,
            stage=self.stage,
            argument=self.argument.details or self.argument.name,
            value=self.value,
        )
        self.event_bus.emit(ItemSelectionViewEvent.ITEM_CHANGED, self)

    def get_editor_value(self):
        if self.uses_package_picker:
            return self.own_packages or None
        buffer = self.text_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        return [line.strip() for line in text.splitlines() if line.strip()] or None

    def set_editor_value(self, value):
        if self.uses_package_picker:
            self.own_packages = list(value or [])
            return
        self.text_view.get_buffer().set_text("\n".join(value or []))

    def normalized_custom_value(self, value):
        if isinstance(value, str):
            value = value.splitlines()
        return [str(item) for item in value if str(item).strip()] or None if value else None

    def custom_value_display(self, value) -> str:
        shown = ", ".join(value[:self.SUBTITLE_MAX_ITEMS])
        hidden_count = len(value) - self.SUBTITLE_MAX_ITEMS
        return f"{shown} (+{hidden_count} more)" if hidden_count > 0 else shown

    def is_modified(self) -> bool:
        return self.get_editor_value() != self.own_value()

    def on_buffer_changed(self, buffer):
        modified = self.is_modified()
        self.apply_button.set_sensitive(modified)
        self.revert_button.set_sensitive(modified)

    def on_key_pressed(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.CONTROL_MASK:
            self.apply_custom_value()
            return True
        return False

    def update_display(self):
        super().update_display()
        # Package settings use a dedicated pushed view; other lists use the multiline text editor.
        if self.uses_package_picker:
            return
        self.hint_label.set_label("Entries added to inherited ones, one per line. Ctrl+Enter to apply." if self.inherit_options()
                                  else "One entry per line. Ctrl+Enter to apply.")
        self.on_buffer_changed(self.text_view.get_buffer())

class StageCacheRow(Adw.ExpanderRow):
    """Selects package or kernel cache of stage: automatic folder in project builds (shared by stages with the same
    rel_type), folder selected by user, or no cache. Value is StageAutomaticOption, folder path or None."""

    def __init__(self, project_directory: ProjectDirectory, stage: ProjectStage, argument: StageArgumentTargetDetails):
        super().__init__()
        self.project_directory = project_directory
        self.stage = stage
        self.argument = argument
        self.value = None
        self._loading = False
        self.event_bus = EventBus[ItemSelectionViewEvent]()
        self.set_title(argument.display_name)
        self.check_buttons: dict[str, Gtk.CheckButton] = {}
        self.automatic_row = self._add_source_row("automatic", "Automatic")
        self.folder_row = self._add_source_row("folder", "Folder")
        self.none_row = self._add_source_row("none", "None", subtitle="Disabled, built packages are not kept" if argument.details == StageArgumentDetails.pkgcache_path else "Disabled, built kernels are not kept")
        choose_button = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Select folder", valign=Gtk.Align.CENTER)
        choose_button.add_css_class("flat")
        choose_button.connect("clicked", lambda button: self._select_folder())
        self.folder_row.add_suffix(choose_button)
        self.load_state()

    def _add_source_row(self, source: str, title: str, subtitle: str | None = None) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle=subtitle or "")
        check_button = Gtk.CheckButton()
        if self.check_buttons:
            check_button.set_group(next(iter(self.check_buttons.values())))
        check_button.connect("toggled", self._on_source_toggled, source)
        row.add_prefix(check_button)
        row.set_activatable_widget(check_button)
        self.add_row(row)
        self.check_buttons[source] = check_button
        return row

    # State:

    def _source(self) -> str:
        if self.value == StageAutomaticOption.GENERATE_AUTOMATICALLY:
            return "automatic"
        return "folder" if self.value else "none"

    def load_state(self):
        value = getattr(self.stage, self.argument.attribute_name, None)
        self.value = value if value == StageAutomaticOption.GENERATE_AUTOMATICALLY or isinstance(value, str) and value else None
        self._folder = self.value if isinstance(self.value, str) else getattr(self, "_folder", None)
        self._loading = True
        self.check_buttons[self._source()].set_active(True)
        self._loading = False
        self.update_display()

    def refresh_options(self):
        """Automatic folder depends on rel_type."""
        self.update_display()

    def update_display(self):
        automatic_path = stage_cache_path(self.project_directory, _StageWithValue(self.stage, self.argument.attribute_name, StageAutomaticOption.GENERATE_AUTOMATICALLY), self.argument.details)
        automatic_display = collapse_home(automatic_path) if automatic_path else "Folder in project builds, shared by stages with the same Rel type"
        self.automatic_row.set_subtitle(GLib.markup_escape_text(automatic_display))
        self.folder_row.set_subtitle(GLib.markup_escape_text(collapse_home(self._folder) if self._folder else "No folder selected"))
        match self._source():
            case "automatic": subtitle = f"Automatic: {automatic_display}" if automatic_path else "Automatic"
            case "folder": subtitle = collapse_home(self.value)
            case _: subtitle = "None"
        self.set_subtitle(GLib.markup_escape_text(subtitle))

    def _set_value(self, value):
        self.value = value
        self.update_display()
        self.event_bus.emit(ItemSelectionViewEvent.ITEM_CHANGED, self)

    # Selecting:

    def _on_source_toggled(self, button: Gtk.CheckButton, source: str):
        if self._loading or not button.get_active():
            return
        match source:
            case "automatic": self._set_value(StageAutomaticOption.GENERATE_AUTOMATICALLY)
            case "none": self._set_value(None)
            case "folder":
                if self._folder:
                    self._set_value(self._folder)
                else:
                    self._select_folder()

    def _select_folder(self):
        dialog = Gtk.FileDialog(title=f"Select {self.argument.display_name.lower()} folder", modal=True)
        if self._folder and os.path.isdir(self._folder):
            dialog.set_initial_folder(Gio.File.new_for_path(self._folder))
        dialog.select_folder(self.get_root(), None, self._on_folder_selected)

    def _on_folder_selected(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            folder = None # Canceled.
        if folder and folder.get_path():
            self._folder = folder.get_path()
            self._loading = True
            self.check_buttons["folder"].set_active(True)
            self._loading = False
            self._set_value(self._folder)
        else:
            # Restore previous selection when folder wasn't selected.
            self._loading = True
            self.check_buttons[self._source()].set_active(True)
            self._loading = False
            self.update_display()

class _StageWithValue:
    """Stage with one argument replaced, used to preview value of option that is not selected."""

    def __init__(self, stage, attribute_name: str, value):
        self._stage = stage
        self._attribute_name = attribute_name
        self._value = value

    def __getattr__(self, name):
        return self._value if name == self._attribute_name else getattr(self._stage, name)

class StageKernelRow(Adw.ExpanderRow):
    """Kernel of stage with its settings. Settings not set by stage use values of releng template, which are shown
    with them. Settings are edited by their type: switches, lists (one entry per line) and texts."""

    def __init__(self, project_directory: ProjectDirectory, stage: ProjectStage, kernel_name: str, on_rename=None):
        super().__init__(title=GLib.markup_escape_text(kernel_name))
        self.project_directory = project_directory
        self.stage = stage
        self.kernel_name = kernel_name
        self._loading = False
        # Name is used by catalyst in file names (/boot/<name>) and names of settings (boot/kernel/<name>/...).
        self.name_row = Adw.EntryRow(title="Name", text=kernel_name, show_apply_button=True)
        self.name_row.set_tooltip_text("Name of kernel, used in names of its files in /boot")
        self.name_row.connect("changed", lambda row: (row.remove_css_class("error"), row.set_tooltip_text("Name of kernel, used in names of its files in /boot")))
        if on_rename:
            self.name_row.connect("apply", lambda row: self._on_rename(row, on_rename))
        self.add_row(self.name_row)
        self.entry_rows = []
        for setting in KERNEL_SETTINGS:
            match setting.type:
                case _ if setting.key == "sources":
                    row = KernelPackageRow(setting.title, on_select=self._on_package_selected)
                case KernelSettingType.BOOLEAN:
                    row = Adw.SwitchRow(title=GLib.markup_escape_text(setting.title))
                    row.connect("notify::active", self._on_switch_toggled, setting)
                case KernelSettingType.LIST:
                    row = KernelListRow(setting, on_apply=lambda values, setting=setting: self._set(setting, values))
                case _:
                    row = Adw.EntryRow(show_apply_button=True)
                    row.connect("apply", lambda row, setting=setting: self._set(setting, row.get_text().strip()))
            set_row_description(row, setting.description)
            self.add_row(row)
            self.entry_rows.append((setting, row))
            if isinstance(row, KernelPackageRow):
                self.add_row(row.custom_row)
        # Distribution kernel is set by kind of package, switch is shown only when it isn't known. Kinds of packages
        # are read in background (cached), switch is hidden until then.
        self.packages = None
        self._advanced = True
        self._changed_only = False
        self._switch_needed = False
        self.load_state()
        def load_packages():
            try:
                packages = load_kernel_packages(project_directory, stage)
            except Exception as e:
                print(f"Failed to read kernel packages: {e}")
                packages = []
            GLib.idle_add(lambda: self._packages_loaded(packages) and False)
        threading.Thread(target=load_packages, daemon=True).start()

    def _packages_loaded(self, packages):
        self.packages = packages
        for setting, row in self.entry_rows:
            if isinstance(row, KernelPackageRow):
                row.set_packages(packages)
        self.load_state()
        self._update_switch_visibility()

    def _derived_distkernel(self) -> bool | None:
        package, _ = kernel_setting(self.project_directory, self.stage, self.kernel_name, "sources")
        return derived_distkernel(package, self.packages) if self.packages is not None else None

    def _update_switch_visibility(self):
        own = own_kernel_settings(self.stage).get(self.kernel_name, {})
        for setting, row in self.entry_rows:
            if setting.key == "distkernel":
                visible = (setting.key in own if self._changed_only else
                           self._advanced or KERNEL_SETTING_LEVELS[setting.key] == StageArgumentLevel.BASIC)
                row.set_visible(visible and self._switch_needed)

    def _on_rename(self, row: Adw.EntryRow, on_rename):
        if error := on_rename(self.kernel_name, row.get_text().strip()):
            row.add_css_class("error")
            row.set_tooltip_text(error)

    def has_stage_overrides(self) -> bool:
        return bool(own_kernel_settings(self.stage).get(self.kernel_name, {}))

    def set_advanced(self, advanced: bool, changed_only: bool = False) -> None:
        """Shows settings appropriate for the selected mode."""
        own = own_kernel_settings(self.stage).get(self.kernel_name, {})
        self._advanced = advanced
        self._changed_only = changed_only
        self.set_visible(not changed_only or bool(own))
        self.name_row.set_visible(not changed_only or bool(own))
        for setting, row in self.entry_rows:
            visible = (setting.key in own if changed_only else
                       advanced or KERNEL_SETTING_LEVELS[setting.key] == StageArgumentLevel.BASIC)
            row.set_visible(visible)
            if setting.key == "sources" and isinstance(row, KernelPackageRow):
                row.custom_row.set_visible(visible and row.is_custom_selected())
        self._update_switch_visibility()

    def load_state(self):
        self._loading = True
        own = own_kernel_settings(self.stage).get(self.kernel_name, {})
        summary = []
        for setting, row in self.entry_rows:
            value, is_own = kernel_setting(self.project_directory, self.stage, self.kernel_name, setting.key)
            inherited = value is not None and not is_own
            match setting.type:
                case _ if setting.key == "sources":
                    row.load(own.get(setting.key), value if inherited else None)
                case KernelSettingType.BOOLEAN:
                    derived = self._derived_distkernel() if setting.key == "distkernel" else None
                    if setting.key == "distkernel":
                        self._switch_needed = derived is None and self.packages is not None
                    if derived is not None:
                        value = "yes" if derived else None
                    row.set_active(kernel_setting_enabled(value))
                    row.set_subtitle("Kind of package is not known, set how catalyst builds it" if setting.key == "distkernel"
                                     else "From releng spec" if inherited else "Set by this stage" if is_own else "")
                case KernelSettingType.LIST:
                    row.load(own.get(setting.key) or [], value if inherited else None)
                case _:
                    row.set_text(self._text(own.get(setting.key)))
                    # Empty entry shows title as placeholder, with value of releng template used then.
                    row.set_title(GLib.markup_escape_text(f"{setting.title} (releng: {self._text(value)})" if inherited else setting.title))
            if setting.key == "sources" and value:
                summary.append(self._text(value))
            elif setting.key == "distkernel":
                summary.append("distribution kernel" if kernel_setting_enabled(value) else "genkernel")
        if hasattr(self, "_advanced"):
            self._update_switch_visibility()
        self.set_subtitle(GLib.markup_escape_text(" · ".join(summary)))
        self._loading = False

    def _on_switch_toggled(self, row: Adw.SwitchRow, _param, setting):
        if self._loading:
            return
        # Stage stores value only when it differs from releng template (no turns off releng yes, and isn't written).
        releng = kernel_setting_enabled(releng_kernel_setting(self.project_directory, self.stage, self.kernel_name, setting.key))
        enabled = row.get_active()
        self._set(setting, None if enabled == releng else "yes" if enabled else "no")

    def _set(self, setting, value):
        set_kernel_setting(self.project_directory, self.stage, self.kernel_name, setting.key, value or None)
        self.load_state()

    def _own_value_for(self, key: str, value):
        """Value stored by stage: none when it's the same as value of releng template."""
        releng = releng_kernel_setting(self.project_directory, self.stage, self.kernel_name, key)
        if key == "distkernel":
            return None if value == kernel_setting_enabled(releng) else "yes" if value else "no"
        return None if value == releng else value

    def _on_package_selected(self, atom: str | None, kind: KernelPackageKind | None):
        """Package of kernel (None uses releng template), Distribution kernel follows kind of package (also typed one,
        recognized by its name)."""
        set_kernel_setting(self.project_directory, self.stage, self.kernel_name, "sources",
                           None if atom is None else self._own_value_for("sources", atom))
        derived = self._derived_distkernel()
        if derived is not None:
            set_kernel_setting(self.project_directory, self.stage, self.kernel_name, "distkernel",
                               self._own_value_for("distkernel", derived))
        self.load_state()

    @staticmethod
    def _text(value) -> str:
        if value is None:
            return ""
        return " ".join(str(item) for item in value) if isinstance(value, list) else str(value)

class KernelListRow(Adw.ExpanderRow):
    """List setting of kernel, edited one entry per line like list settings of stage. Empty list uses value of
    releng template, shown in subtitle."""

    def __init__(self, setting, on_apply):
        super().__init__(title=GLib.markup_escape_text(setting.title))
        self.on_apply = on_apply
        self.values: list[str] = []
        self._loaded = False
        self.text_view = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False,
                                      top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        self.text_view.set_size_request(-1, 72)
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.text_view.add_controller(key_controller)
        hint = Gtk.Label(label="One entry per line. Ctrl+Enter to apply.", halign=Gtk.Align.START, hexpand=True)
        hint.add_css_class("dimmed")
        hint.add_css_class("caption")
        revert_button = Gtk.Button(label="Revert")
        revert_button.connect("clicked", lambda _: self._set_text(self.values))
        apply_button = Gtk.Button(label="Apply")
        apply_button.add_css_class("suggested-action")
        apply_button.connect("clicked", lambda _: self._apply())
        buttons = Gtk.Box(spacing=6)
        for widget in (hint, revert_button, apply_button):
            buttons.append(widget)
        editor = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        editor.append(Gtk.Frame(child=self.text_view))
        editor.append(buttons)
        self.add_row(Gtk.ListBoxRow(activatable=False, selectable=False, child=editor))

    def load(self, values: list[str], inherited: list[str] | None):
        # Text is replaced only when value changed, edits not applied yet stay (and page doesn't move).
        if list(values) != self.values or not self._loaded:
            self._set_text(list(values))
        self._loaded = True
        self.values = list(values)
        if self.values:
            subtitle = ", ".join(self.values)
        elif inherited:
            subtitle = f"From releng spec: {', '.join(inherited)}"
        else:
            subtitle = "None"
        self.set_subtitle(GLib.markup_escape_text(subtitle))

    def _set_text(self, values: list[str]):
        self.text_view.get_buffer().set_text("\n".join(values))

    def _apply(self):
        buffer = self.text_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self.on_apply([line.strip() for line in text.splitlines() if line.strip()])

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.CONTROL_MASK:
            self._apply()
            return True
        return False

def _set_customized_style(widget: Gtk.Widget, customized: bool):
    if customized:
        widget.add_css_class("customized-setting")
    else:
        widget.remove_css_class("customized-setting")
    # Expander rows (for example Version stamp) render their title in a nested ActionRow. Apply the class there too so
    # only the setting title gets the customized emphasis, never the option/value labels inside the expander.
    if isinstance(widget, Adw.ExpanderRow):
        def find_header(container):
            child = container.get_first_child()
            while child is not None:
                if isinstance(child, Gtk.Revealer):
                    return None
                if isinstance(child, Adw.ActionRow):
                    return child
                if found := find_header(child):
                    return found
                child = child.get_next_sibling()
            return None
        header = find_header(widget)
        if header:
            if customized:
                header.add_css_class("customized-setting")
            else:
                header.remove_css_class("customized-setting")
