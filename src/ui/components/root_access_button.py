import sys
from gi.repository import Gtk, Adw
from .app_events import AppEvents, app_event_bus
from .root_helper_client import RootHelperClient, RootHelperClientEvents, ServerCall, ServerCallEvents, AuthorizationKeeper
from .settings import *
from .root_command_output_view import RootCommandOutputView
from .rootless import rootless_toolset_unsupported_reason

class RootAccessButton(Gtk.Overlay):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.displayed_requests_views = []

        # Create the button and spinner
        self.root_access_button = Gtk.Button.new()
        self.root_access_spinner = Adw.Spinner()

        # Set up the button
        self.root_access_button.set_tooltip_text("Toggle root access")
        self.root_access_button.get_style_context().add_class("circular")
        self.root_access_button.get_style_context().add_class("image-button")
        self.root_access_button.set_focusable(False)

        # Set up the spinner
        self.root_access_spinner.set_can_target(False)
        self.root_access_spinner.set_visible(RootHelperClient.shared().running_actions)

        # Add the button and spinner to the overlay
        self.set_child(self.root_access_button)
        self.add_overlay(self.root_access_spinner)

        # Create popover to display when the button is clicked
        self.popover = Gtk.Popover.new()
        self.popover.set_parent(self)
        self.popover.set_focusable(False)

        # Popover task list
        self.task_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.task_list_box.set_focusable(False)
        self.task_list_box.set_vexpand(True)
        self.popover.set_child(self.task_list_box)

        # Popover close button
        self.stop_button = Gtk.Button()
        self.stop_button.get_style_context().add_class("destructive-action")
        self.stop_button.set_focusable(False)
        self.stop_button.set_label("Disable root access")
        self.stop_button.connect("clicked", self.disable_root_access)
        self.task_list_box.append(self.stop_button)

        # Popover start button
        self.start_button = Gtk.Button()
        self.start_button.get_style_context().add_class("suggested-action")
        self.start_button.set_focusable(False)
        self.start_button.set_label("Enable root access")
        self.start_button.connect("clicked", self.enable_root_access)
        self.start_button.set_sensitive(not RootHelperClient.shared().running_actions)
        self.task_list_box.append(self.start_button)

        # Stop warning label
        #self.stop_warning_label = Gtk.Label(label="Disabling root access while actions are in progress will terminate these action.")
        #self.stop_warning_label.get_style_context().add_class("caption-heading")
        #self.stop_warning_label.get_style_context().add_class("dim-label")
        #self.stop_warning_label.set_halign(Gtk.Align.CENTER)
        #self.stop_warning_label.set_margin_top(12)
        #self.stop_warning_label.set_margin_bottom(6)
        #self.stop_warning_label.set_wrap(True)
        #self.stop_warning_label.set_visible(RootHelperClient.shared().running_actions and RootHelperClient.shared().is_server_process_running)
        #self.task_list_box.append(self.stop_warning_label)

        # CheckButton for "Keep root access unlocked"
        self.keep_unlocked_checkbox = Gtk.CheckButton(label="Keep unlocked")
        self.keep_unlocked_checkbox.get_style_context().add_class("caption-heading")
        self.keep_unlocked_checkbox.set_active(Repository.Settings.value.keep_root_unlocked)
        self.keep_unlocked_checkbox.set_focusable(False)
        self.keep_unlocked_checkbox.connect("toggled", self.on_keep_unlocked_toggled)
        self.keep_unlocked_checkbox.set_margin_top(6)
        self.task_list_box.append(self.keep_unlocked_checkbox)

        # Divider
        #self.root_tasks_separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        #self.root_tasks_separator.set_visible(RootHelperClient.shared().running_actions)
        #self.root_tasks_separator.set_margin_top(12)
        #self.task_list_box.append(self.root_tasks_separator)

        # "Root tasks:" label (initially hidden)
        self.root_tasks_label = Gtk.Label(label="Root tasks:")
        self.root_tasks_label.get_style_context().add_class("caption-heading")
        self.root_tasks_label.get_style_context().add_class("dim-label")
        self.root_tasks_label.set_halign(Gtk.Align.START)
        self.root_tasks_label.set_margin_top(12)
        self.root_tasks_label.set_margin_bottom(6)
        self.root_tasks_label.set_visible(RootHelperClient.shared().running_actions)
        self.task_list_box.append(self.root_tasks_label)

        # Add initial requests to list
        for request in RootHelperClient.shared().running_actions:
            self.add_request_to_list(request)

        # Event handling for the button click
        self.root_access_button.connect("clicked", self.toggle_root_access)

        # Subscribe to events
        RootHelperClient.shared().event_bus.subscribe(RootHelperClientEvents.CHANGE_ROOT_ACCESS, self.root_access_changed)
        RootHelperClient.shared().event_bus.subscribe(RootHelperClientEvents.ROOT_REQUEST_STATUS, self.root_requests_status_changed)
        Repository.Settings.value.event_bus.subscribe(SettingsEvents.KEEP_ROOT_UNLOCKED_CHANGED, self.keep_root_unlocked_changed)

        # Set initial state based on root access status
        self.root_access_changed(RootHelperClient.shared().is_server_process_running)
        self.update_visibility()

    def update_visibility(self):
        """Hidden when everything runs without root (in user namespace). Still shown while root helper is running, eg.
        when some action needed it anyway."""
        client = RootHelperClient.shared()
        # Root helper works only on Linux, elsewhere toolsets run in virtual machines.
        self.set_visible(
            (sys.platform.startswith("linux") and rootless_toolset_unsupported_reason() is not None)
            or client.is_server_process_running
            or bool(client.running_actions)
        )

    def toggle_root_access(self, sender):
        """Toggle root access state and show the popover."""
        if RootHelperClient.shared().is_server_process_running or RootHelperClient.shared().running_actions:
            self.popover.show()
        else:
            RootHelperClient.shared().authorize_and_run(callback=self.manual_authorization_handler)

    def manual_authorization_handler(self, authorization_keeper: AuthorizationKeeper | None):
        if authorization_keeper:
            # Prevents instant disable of authorization
            authorization_keeper.ignore_released = True

    def root_access_changed(self, enabled: bool):
        """Handle changes to root access state."""
        if enabled:
            self.root_access_button.get_style_context().add_class("destructive-action")
            icon = Gtk.Image.new_from_icon_name("changes-allow-symbolic")
            self.root_access_button.set_child(icon)
        else:
            self.root_access_button.get_style_context().remove_class("destructive-action")
            icon = Gtk.Image.new_from_icon_name("changes-prevent-symbolic")
            self.root_access_button.set_child(icon)
        self.start_button.set_visible(not enabled)
        self.stop_button.set_visible(enabled)
        self.update_visibility()
        #self.stop_warning_label.set_visible(RootHelperClient.shared().running_actions and enabled)

    def root_requests_status_changed(self, client: RootHelperClient, call: ServerCall, status: bool):
        """Handle changes to root access request status."""
        self.root_access_spinner.set_visible(client.running_actions)
        if status:
            self.add_request_to_list(call)
        else:
            self.remove_request_from_list(call)
        self.start_button.set_sensitive(not RootHelperClient.shared().running_actions)
        self.root_tasks_label.set_visible(RootHelperClient.shared().running_actions)
        self.update_visibility()
        #self.stop_warning_label.set_visible(RootHelperClient.shared().running_actions and RootHelperClient.shared().is_server_process_running)
        #self.root_tasks_separator.set_visible(RootHelperClient.shared().running_actions)

    def disable_root_access(self, sender):
        """Disable root access when the button is clicked."""
        RootHelperClient.shared().stop_root_helper()
        self.popover.hide()

    def enable_root_access(self, sender):
        """Enable root access when the button is clicked."""
        RootHelperClient.shared().authorize_and_run(callback=self.manual_authorization_handler)
        self.popover.hide()

    def add_request_to_list(self, call: ServerCall):
        action_row = RootActionInfoRow(call=call, parent=self)
        self.task_list_box.append(action_row)
        self.displayed_requests_views.append(action_row)

    def remove_request_from_list(self, call: ServerCall):
        for row in self.displayed_requests_views:
            if row.call == call:
                self.task_list_box.remove(row)
                self.displayed_requests_views.remove(row)
                break

    def on_keep_unlocked_toggled(self, button):
        Repository.Settings.value.keep_root_unlocked = button.get_active()

    def keep_root_unlocked_changed(self, value: bool):
        if value != self.keep_unlocked_checkbox.get_active():
            self.keep_unlocked_checkbox.set_active(value)

class RootActionInfoRow(Gtk.Box):
    def __init__(self, call: ServerCall, parent: RootAccessButton):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.call = call
        self.parent = parent
        self.set_hexpand(True)

        # Divider (separator at the top)
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        self.append(separator)

        # Inner horizontal row layout
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row_box.set_hexpand(True)
        row_box.set_valign(Gtk.Align.CENTER)

        # Create label on the left
        self.name_button = Gtk.Button(label=call.request.function_name)
        #self.name_button.set_hexpand(True)
        self.name_button.get_style_context().add_class("flat")
        #self.name_button.set_can_focus(False)
        #self.name_button.set_focusable(False)
        self.name_button.connect("clicked", self.show_output)
        row_box.append(self.name_button)

        # Icon button on the right
        icon = Gtk.Image.new_from_icon_name("window-close-symbolic")
        self.button = Gtk.Button()
        self.button.set_child(icon)
        self.button.set_valign(Gtk.Align.CENTER)
        self.button.set_focusable(False)
        self.button.get_style_context().add_class("circular")
        self.button.get_style_context().add_class("flat")
        self.button.set_margin_top(4)
        self.button.set_margin_bottom(4)
        self.button.connect("clicked", self.close_call)
        if not self.call.is_cancellable:
            self.button.set_opacity(0.0)
        row_box.append(self.button)

        # Add the horizontal row to the vertical box
        self.append(row_box)

        self.mark_terminating(call.terminated)

        self.call.event_bus.subscribe(ServerCallEvents.CALL_WILL_TERMINATE, self.call_will_terminate)

    def close_call(self, button: Gtk.Button):
        self.call.cancel()
        self.mark_terminating() # Marks right away without waiting for server signal.

    def show_output(self, button: Gtk.Button):
        self.parent.popover.hide()
        command_output_view = RootCommandOutputView(call=self.call)
        app_event_bus.emit(AppEvents.PRESENT_VIEW, command_output_view, self.call.request.function_name, 640, 480)

    def call_will_terminate(self):
       self.mark_terminating()

    def mark_terminating(self, terminating: bool = True):
        if terminating:
            self.name_button.get_style_context().add_class("dim-label")
        else:
            self.name_button.get_style_context().remove_class("dim-label")
        if self.button:
            self.button.set_sensitive(not terminating and self.call.is_cancellable)
        self.name_button.set_sensitive(not terminating)

