from gi.repository import Gtk, GObject, Adw
from .repository import Repository, RepositoryEvent
from . import repositories as _repositories  # Register repository types before resolving them by name.
from .multistage_process import MultiStageProcess, MultiStageProcessEvent, MultiStageProcessState
from .status_indicator import StatusIndicator
from .event_bus import SharedEvent
from .settings import SettingsEvents

# Import additional classed so that it can be parsed in repository_list_view:
from .toolset_installation  import ToolsetInstallation
from .snapshot_installation import SnapshotInstallation
from .releng_installation   import RelengInstallation
from .overlay_installation  import OverlayInstallation
from .project_installation  import ProjectInstallation
from .project_build_process import ProjectBuild
from .build_machine_installation import VMRunnerInstallation
from .deploy_installation import DeployInstallation

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/components/repository_list/repository_list_view.ui')
class RepositoryListView(Gtk.Box):
    __gtype_name__ = "RepositoryListView"

    __gsignals__ = {
        "item-row-pressed":         (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
        "installation-row-pressed": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
        "add-new-item-pressed":     (GObject.SignalFlags.RUN_FIRST, None, ())
    }

    # View elements:
    items_container  = Gtk.Template.Child()
    preference_group = Gtk.Template.Child()
    add_item_container = Gtk.Template.Child()
    add_item_row = Gtk.Template.Child()
    # Properties:
    title                          = GObject.Property(type=str, default=None)
    description                    = GObject.Property(type=str, default=None)
    item_class_name                = GObject.Property(type=str, default=None)
    item_installation_class_name   = GObject.Property(type=str, default=None)
    item_icon                      = GObject.Property(type=str, default=None)
    item_title_property_name       = GObject.Property(type=str, default=None)
    item_subtitle_property_name    = GObject.Property(type=str, default=None)
    item_status_property_name      = GObject.Property(type=str, default=None)
    item_unsupported_property_name = GObject.Property(type=str, default=None)
    show_add_button                = GObject.Property(type=bool, default=True)
    add_button_title               = GObject.Property(type=str, default="Add item")
    show_installations             = GObject.Property(type=bool, default=True) # Rows of started processes of item_installation_class.
    preserve_item_order            = GObject.Property(type=bool, default=False)
    show_default_toolset           = GObject.Property(type=bool, default=False)
    show_default_runner            = GObject.Property(type=bool, default=False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.connect("realize", self.on_realize)

    def on_realize(self, widget):
        self.preference_group.set_title(self.title)
        self.preference_group.set_description(self.description or None)
        self.add_item_container.set_visible(self.show_add_button)
        self.add_item_row.set_title(self.add_button_title)
        self.item_installation_class = globals().get(self.item_installation_class_name)
        self.repository = getattr(Repository, self.item_class_name)
        # Setup items entries
        self._load_items()
        # Subscribe to relevant events
        self.repository.event_bus.subscribe(
            RepositoryEvent.VALUE_CHANGED,
            self.items_updated
        )
        if self.show_default_toolset:
            Repository.Settings.value.event_bus.subscribe(
                SettingsEvents.DEFAULT_TOOLSET_CHANGED,
                self.items_updated
            )
        if self.show_default_runner:
            Repository.Settings.value.event_bus.subscribe(
                SettingsEvents.DEFAULT_RUNNER_CHANGED,
                self.items_updated
            )
        MultiStageProcess.event_bus.subscribe(
            MultiStageProcessEvent.STARTED_PROCESSES_CHANGED,
            self.items_installations_updated
        )

    def items_installations_updated(
        self,
        process_class: type[MultiStageProcess],
        started_processes: list[MultiStageProcess]
    ):
        if self.item_installation_class is not None and issubclass(process_class, self.item_installation_class):
            self._load_items(started_processes=started_processes)

    def items_updated(self, _):
        self._load_items()

    def _load_items(self, started_processes: list[MultiStageProcess] | None = None):
        if started_processes is None:
            # Lists without installations (no installation class) show only items.
            started_processes = MultiStageProcess.get_started_processes_by_class(self.item_installation_class) if self.item_installation_class else []
        # Remove previously added rows
        if hasattr(self, "_item_rows"):
            for row in self._item_rows:
                self.items_container.remove(row)
        self._item_rows = []

        items = self.repository.value
        default_toolset_group = Gtk.CheckButton() if self.show_default_toolset else None
        default_runner_group = getattr(self, "default_runner_group", None)
        if self.show_default_runner and default_runner_group is None:
            default_runner_group = Gtk.CheckButton()
        if self.preserve_item_order:
            # Rows are inserted at index 0 below, so reverse iteration preserves the repository's order on screen.
            items = reversed(items)
        for item in items:
            try:
                item_row = ItemRow(
                    item                           = item,
                    item_title_property_name       = self.item_title_property_name,
                    item_subtitle_property_name    = self.item_subtitle_property_name,
                    item_status_property_name      = self.item_status_property_name,
                    item_unsupported_property_name = self.item_unsupported_property_name,
                    item_icon                      = self.item_icon,
                    default_toolset_group           = default_toolset_group,
                    is_default_toolset              = (str(item.uuid) == Repository.Settings.value.default_toolset_id
                                                       if self.show_default_toolset else False),
                    on_default_toolset_selected     = self._on_default_toolset_selected if self.show_default_toolset else None,
                    default_runner_group           = default_runner_group,
                    is_default_runner               = (str(item.id) == Repository.Settings.value.default_runner_id
                                                       if self.show_default_runner else False),
                    on_default_runner_selected      = self._on_default_runner_selected if self.show_default_runner else None
                )
            except Exception as error:
                try:
                    name = item.name
                except Exception:
                    name = "Unknown item"
                item_row = self._broken_item_row(name, error)
            item_row.set_activatable(True)
            if getattr(item_row, "is_broken_item", False):
                item_row.set_activatable(False)
            else:
                icon = Gtk.Image.new_from_icon_name("go-next-symbolic")
                icon.add_css_class("dimmed")
                item_row.add_suffix(icon)
                item_row.connect("activated", self.on_item_row_pressed)
            self.items_container.insert(item_row, 0)
            self._item_rows.append(item_row)

        for installation in (started_processes if self.show_installations else []):
            installation_row = ItemInstallationRow(installation, self.item_icon)
            installation_row.connect(
                "activated", self.on_installation_row_pressed
            )
            self.items_container.insert(installation_row, 0)
            self._item_rows.append(installation_row)

        self.items_container.set_visible(self._item_rows)

    def _on_default_toolset_selected(self, toolset):
        if str(toolset.uuid) != Repository.Settings.value.default_toolset_id:
            Repository.Settings.value.default_toolset_id = str(toolset.uuid)

    def _on_default_runner_selected(self, runner):
        if str(runner.id) != Repository.Settings.value.default_runner_id:
            Repository.Settings.value.default_runner_id = str(runner.id)

    @staticmethod
    def _broken_item_row(name: str, error: Exception | str) -> Adw.ActionRow:
        detail = str(error)
        row = Adw.ActionRow(title=str(name), subtitle=f"Broken · {detail}")
        row.add_css_class("warning")
        row.set_tooltip_text(detail)
        row.set_activatable(False)
        icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        icon.set_pixel_size(24)
        icon.add_css_class("warning")
        row.add_prefix(icon)
        row.is_broken_item = True
        return row

    def on_item_row_pressed(self, sender):
        self.emit("item-row-pressed", sender.item)

    def on_installation_row_pressed(self, sender):
        self.emit("installation-row-pressed", sender.installation)

    @Gtk.Template.Callback()
    def on_add_item_activated(self, sender):
        self.emit("add-new-item-pressed")

class ItemRow(Adw.ActionRow):

    def __init__(
        self,
        item,
        item_title_property_name,
        item_subtitle_property_name,
        item_status_property_name,
        item_unsupported_property_name,
        item_icon,
        item_icon_resolver = None,
        default_toolset_group = None,
        is_default_toolset = False,
        on_default_toolset_selected = None,
        default_runner_group = None,
        is_default_runner = False,
        on_default_runner_selected = None,
    ):
        # Icon of item itself (icon_name attribute or resolver) is used instead of icon of list.
        icon_name = item_icon_resolver(item) if item_icon_resolver else None
        super().__init__(
            title     = getattr(item, item_title_property_name, None) if item_title_property_name else item if isinstance(item, str) else None,
            subtitle  = getattr(item, item_subtitle_property_name, None) if item_subtitle_property_name else None,
            icon_name = icon_name or getattr(item, "icon_name", None) or item_icon
        )
        self.item = item
        self.item_status_property_name = item_status_property_name
        self.item_subtitle_property_name = item_subtitle_property_name
        self.item_unsupported_property_name = item_unsupported_property_name
        if default_toolset_group is not None:
            self.default_toolset_button = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            self.default_toolset_button.set_group(default_toolset_group)
            self.default_toolset_button.set_active(is_default_toolset)
            self.default_toolset_button.set_tooltip_text("Use this as the default toolset for actions")
            self.default_toolset_button.connect("toggled", self._on_default_toolset_toggled,
                                                on_default_toolset_selected)
            self.add_suffix(self.default_toolset_button)
        if default_runner_group is not None:
            self.default_runner_button = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            self.default_runner_button.set_group(default_runner_group)
            self.default_runner_button.set_active(is_default_runner)
            self.default_runner_button.set_tooltip_text("Use this as the default runner for actions")
            self.default_runner_button.connect("toggled", self._on_default_runner_toggled,
                                               on_default_runner_selected)
            self.add_suffix(self.default_runner_button)
        # Unsupported status:
        if self.item_unsupported_property_name:
            if getattr(self.item, self.item_unsupported_property_name, False):
                self.add_css_class('warning')
            else:
                self.remove_css_class('warning')
        # Status indicator
        self.status_indicator = StatusIndicator()
        self.status_indicator.set_margin_start(6)
        self.status_indicator.set_margin_end(6)
        self.add_suffix(self.status_indicator)
        self.setup_status_indicator()
        # Observe state related event
        if hasattr(self.item, 'event_bus'):
            self.item.event_bus.subscribe(
                SharedEvent.STATE_UPDATED,
                self.state_updated
            )

    def state_updated(self, object):
        if object == self.item:
            self.set_subtitle(
                getattr(self.item, self.item_subtitle_property_name, None)
            )
            self.setup_status_indicator()

    def _on_default_toolset_toggled(self, button, callback):
        if button.get_active() and callback:
            callback(self.item)

    def _on_default_runner_toggled(self, button, callback):
        if button.get_active() and callback:
            callback(self.item)

    def setup_status_indicator(self):
        if self.item_status_property_name is None or not hasattr(self.item, self.item_status_property_name):
            self.status_indicator.set_visible(False)
            return
        status_indicator_values = getattr(
            self.item, self.item_status_property_name
        )
        self.status_indicator.set_visible(True)
        self.status_indicator.set_values(status_indicator_values)

class ItemInstallationRow(Adw.ActionRow):

    def __init__(self, installation: MultiStageProcess, item_icon):
        super().__init__(title=installation.name(), icon_name=item_icon)
        self.installation = installation
        self.set_activatable(True)
        self.progress_label = Gtk.Label()
        self.progress_label.add_css_class("dim-label")
        self.progress_label.add_css_class("caption")
        self.add_suffix(self.progress_label)
        self._set_status(status=installation.status)
        self._set_progress_label(installation.progress)
        installation.event_bus.subscribe(
            MultiStageProcessEvent.STATE_CHANGED,
            self._set_status
        )
        installation.event_bus.subscribe(
            MultiStageProcessEvent.PROGRESS_CHANGED,
            self._set_progress_label
        )

    def _set_progress_label(self, progress):
        self.progress_label.set_label(f"{int(progress * 100)}%")

    def _set_status(self, status: MultiStageProcessState):
        if not hasattr(self, "status_icon"):
            self.status_icon = Gtk.Image()
            self.status_icon.set_pixel_size(24)
            self.add_suffix(self.status_icon)
        status_props = {
            MultiStageProcessState.SETUP:       (False, "", "", "Preparing installation"),
            MultiStageProcessState.IN_PROGRESS: (False, "", "", "Installation in progress"),
            MultiStageProcessState.FAILED:      (True, "error-box-svgrepo-com-symbolic", "error", "Installation failed"),
            MultiStageProcessState.COMPLETED:   (True, "check-square-svgrepo-com-symbolic", "success", "Installation completed"),
        }
        visible, icon_name, style, subtitle = status_props[status]
        self.progress_label.set_visible(not visible)
        self.status_icon.set_visible(visible)
        self.status_icon.set_from_icon_name(icon_name)
        self.set_subtitle(subtitle)
        if hasattr(self.status_icon, 'used_css_class'):
            self.status_icon.remove_css_class(self.used_css_class)
        if style:
            self.status_icon.used_css_class = style
            self.status_icon.add_css_class(style)

