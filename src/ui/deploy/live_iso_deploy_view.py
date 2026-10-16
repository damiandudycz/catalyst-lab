from __future__ import annotations
import os, sys, threading
from dataclasses import dataclass
from enum import Enum
from gi.repository import Gtk, Adw, GLib, Gio
from .deploy_live_iso import (
    LiveIsoDeployment, LiveIsoTarget, RaspberryPiBootSettings, supports_raspberry_pi_iso, raspberry_pi_iso_path,
    live_iso_kernel_warning
)
from .deploy_target import TargetMachine, TargetFirmware, format_size, machine_architecture
from .deploy_image import image_supported_reason
from .local_disk_connection import LocalDiskConnection
from .item_select_view import ItemSelectionViewEvent
from .deploy_installation import DeploySystemSettings
from .deploy_system import LocalizationSettings, WifiSettings, local_localization, local_public_keys

class LiveIsoBoot(Enum):
    """How live CD is started."""
    AS_BUILT = "as-built"
    RASPBERRY_PI = "raspberry-pi"

@dataclass
class LiveIsoMethod:
    """Where live CD is deployed, with reason when it can't be used for this ISO or on this computer."""
    target: LiveIsoTarget
    title: str
    description: str
    unavailable_reason: str | None = None

    @property
    def unavailable(self) -> bool:
        return self.unavailable_reason is not None

    @property
    def details(self) -> str:
        return self.unavailable_reason or self.description

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/deploy/live_iso_deploy_view.ui')
class LiveIsoDeployView(Gtk.Box):
    """Wizard deploying live CD build (ISO image): to disk of this computer (Linux only), or as new ISO file. ISO with
    Raspberry Pi kernel can be prepared for Raspberry Pi firmware, with its boot options."""
    __gtype_name__ = "LiveIsoDeployView"

    wizard_view = Gtk.Template.Child()
    method_page = Gtk.Template.Child()
    method_selection_view = Gtk.Template.Child()
    boot_page = Gtk.Template.Child()
    boot_group = Gtk.Template.Child()
    raspberry_pi_group = Gtk.Template.Child()
    serial_console_row = Gtk.Template.Child()
    older_models_row = Gtk.Template.Child()
    kernel_arguments_row = Gtk.Template.Child()
    config_row = Gtk.Template.Child()
    system_page = Gtk.Template.Child()
    system_box = Gtk.Template.Child()
    disk_page = Gtk.Template.Child()
    disks_group = Gtk.Template.Child()
    find_disks_button = Gtk.Template.Child()
    disk_status_label = Gtk.Template.Child()

    def __init__(self, project_directory=None, build=None, installation_in_progress: LiveIsoDeployment | None = None,
                 content_navigation_view: Adw.NavigationView | None = None):
        super().__init__()
        self.installation_in_progress = installation_in_progress
        self.content_navigation_view = content_navigation_view
        self.project_directory = project_directory or (installation_in_progress.project_directory if installation_in_progress else None)
        self.build = build if build else installation_in_progress.build if installation_in_progress else None
        self.connection: LocalDiskConnection | None = None
        self.machine: TargetMachine | None = None
        self.selected_disk = None
        self.disk_rows: list[Adw.ActionRow] = []
        self.searching = False
        self.raspberry_pi = bool(self.build) and supports_raspberry_pi_iso(self.project_directory, self.build)
        # Boot options: as built, and Raspberry Pi firmware for ISO with Raspberry Pi kernel.
        self.boot_checks: dict[LiveIsoBoot, Gtk.CheckButton] = {}
        group = None
        boots = [LiveIsoBoot.AS_BUILT] + ([LiveIsoBoot.RASPBERRY_PI] if self.raspberry_pi else [])
        for boot in boots:
            title, subtitle = {
                LiveIsoBoot.AS_BUILT: ("As built", "ISO boots like it was built by catalyst (GRUB on UEFI machines)"),
                LiveIsoBoot.RASPBERRY_PI: ("Raspberry Pi", "Boot partition for Raspberry Pi firmware is added, kernel of live CD is for Raspberry Pi"),
            }[boot]
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=boot == boots[-1])
            if group:
                check.set_group(group)
            group = group or check
            check.connect("toggled", self._on_boot_changed)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            self.boot_group.add(row)
            self.boot_checks[boot] = check
        # Lines added to config.txt, edited in text view.
        self.config_view = Gtk.TextView(monospace=True, top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        self.config_view.get_buffer().set_text(RaspberryPiBootSettings().config_lines)
        self.config_view.set_size_request(-1, 72)
        self.config_row.add_row(Gtk.ListBoxRow(activatable=False, selectable=False, child=Gtk.Frame(
            child=self.config_view, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)))
        self._setup_system_page()
        self.methods = self._methods()
        self.method_selection_view.set_static_list(self.methods)
        self.method_selection_view.event_bus.subscribe(ItemSelectionViewEvent.ITEM_CHANGED, self.on_method_changed)
        self._on_boot_changed(None)
        self.on_method_changed(None)
        if self.build:
            size = os.path.getsize(self.build.artifact_path) if os.path.isfile(self.build.artifact_path or "") else 0
            self.wizard_view.set_property("welcome_screen_description",
                f"Writes {self.build.stage_name} ({self.build.artifact}, {format_size(size)}) to USB drive or SD card"
                + (" connected to this computer" if sys.platform.startswith("linux") else "")
                + ", or saves it as new ISO file to write later" + (" (for Raspberry Pi)" if self.raspberry_pi else "") + "."
                + (f"\n\nWarning: {warning}" if (warning := live_iso_kernel_warning(self.build)) else ""))
        self.connect("realize", self.on_realize)
        self.connect("unrealize", self.on_unrealize)

    # Live system settings (empty ones keep live CD as it is)

    def _setup_system_page(self):
        # Status is updated by rows below while they are filled.
        self.system_status_label = Gtk.Label(wrap=True, xalign=0, visible=False)
        self.system_status_label.add_css_class("caption")
        self.system_status_label.add_css_class("error")
        def group(title: str, description: str | None = None) -> Adw.PreferencesGroup:
            widget = Adw.PreferencesGroup(title=title, description=description)
            self.system_box.append(widget)
            return widget
        def entry(group_widget, title: str, password: bool = False):
            row = (Adw.PasswordEntryRow if password else Adw.EntryRow)(title=title)
            row.connect("changed", self._on_system_changed)
            (group_widget.add_row if isinstance(group_widget, Adw.ExpanderRow) else group_widget.add)(row)
            return row
        system = group("Live system", "Settings changed in live system of ISO. Empty ones keep it as it's built. Live "
                       "system is unpacked and packed again, which takes several minutes.")
        self.root_password_row = entry(system, "Root password", password=True)
        self.root_password_confirm_row = entry(system, "Confirm root password", password=True)
        self.ssh_row = Adw.SwitchRow(title="Start SSH server", subtitle="Remote login, with root password or SSH keys")
        self.ssh_row.connect("notify::active", self._on_system_changed)
        system.add(self.ssh_row)
        wifi = group("Wi-Fi", "Network connected automatically when live system starts (NetworkManager). Password is "
                     "stored in ISO, readable by anyone who has the ISO file or drive.")
        self.wifi_ssid_row = entry(wifi, "Network name")
        self.wifi_password_row = entry(wifi, "Password, empty for open network", password=True)
        self.wifi_hidden_row = Adw.SwitchRow(title="Hidden network", subtitle="Network doesn't broadcast its name")
        self.wifi_hidden_row.connect("notify::active", self._on_system_changed)
        wifi.add(self.wifi_hidden_row)
        self.wifi_country_row = entry(wifi, "Country code, eg. PL (channels allowed in country)")
        self.keys_group = group("SSH keys", "Keys of this computer allowed to log in as root.")
        self.key_checks: list[tuple[Gtk.CheckButton, str]] = []
        for key in local_public_keys():
            check = Gtk.CheckButton(active=False, valign=Gtk.Align.CENTER)
            check.connect("toggled", self._on_system_changed)
            row = Adw.ActionRow(title=GLib.markup_escape_text(key.title), subtitle=GLib.markup_escape_text(key.subtitle))
            row.add_prefix(check)
            row.set_activatable_widget(check)
            self.keys_group.add(row)
            self.key_checks.append((check, key.key))
        if not self.key_checks:
            self.keys_group.add(Adw.ActionRow(title="No SSH keys found in ~/.ssh"))
        localization = group("Localization")
        self.localization_row = Adw.ExpanderRow(title="Change localization", subtitle="Suggested from this computer",
                                                show_enable_switch=True, enable_expansion=False)
        self.localization_row.connect("notify::enable-expansion", self._on_system_changed)
        localization.add(self.localization_row)
        suggested = local_localization()
        self.timezone_row = entry(self.localization_row, "Timezone")
        self.keymap_row = entry(self.localization_row, "Keyboard layout")
        self.locales_row = entry(self.localization_row, "Locales, first is system language")
        self.timezone_row.set_text(suggested.timezone)
        self.keymap_row.set_text(suggested.keymap)
        self.locales_row.set_text(", ".join(suggested.locales))
        self.system_box.append(self.system_status_label)

    def _on_system_changed(self, *args):
        error = self._system_error()
        self.system_status_label.set_visible(error is not None)
        self.system_status_label.set_label(error or "")
        self.wizard_view._refresh_buttons_state()

    def _localization(self) -> LocalizationSettings:
        if not self.localization_row.get_enable_expansion():
            return LocalizationSettings()
        locales = [locale for locale in self.locales_row.get_text().replace(",", " ").split() if locale]
        return LocalizationSettings(timezone=self.timezone_row.get_text().strip(), locales=locales,
                                    keymap=self.keymap_row.get_text().strip())

    def _wifi(self) -> WifiSettings | None:
        ssid = self.wifi_ssid_row.get_text()
        if not ssid.strip():
            return None
        return WifiSettings(ssid=ssid, password=self.wifi_password_row.get_text(), hidden=self.wifi_hidden_row.get_active(),
                            country=self.wifi_country_row.get_text().strip())

    def _system_error(self) -> str | None:
        if (wifi := self._wifi()) is not None and (error := wifi.error()):
            return error
        if self.wifi_password_row.get_text() and self._wifi() is None:
            return "Set Wi-Fi network name"
        if self.root_password_row.get_text() != self.root_password_confirm_row.get_text():
            return "Root passwords don't match"
        return self._localization().error()

    def _system_settings(self) -> DeploySystemSettings | None:
        """Settings applied to live system, None when nothing is changed."""
        settings = DeploySystemSettings(
            root_password=self.root_password_row.get_text() or None,
            enable_ssh=self.ssh_row.get_active(),
            ssh_keys=[key for check, key in self.key_checks if check.get_active()],
            localization=self._localization(),
            wifi=self._wifi(),
        )
        localization = settings.localization
        changed = (settings.wifi or settings.root_password or settings.enable_ssh or settings.ssh_keys
                   or localization.timezone or localization.locales or localization.keymap)
        return settings if changed else None

    def _methods(self) -> list[LiveIsoMethod]:
        disk_reason = None if sys.platform.startswith("linux") else \
            "Not available: needs Linux, virtual machines on this system can't access disks of this computer"
        iso_reason = image_supported_reason(self.project_directory) # Tools run in virtual machine on other systems.
        return [
            LiveIsoMethod(LiveIsoTarget.LOCAL_DISK, "Disk of this computer",
                          "USB drive or SD card connected to this computer, booted later on other machine", disk_reason),
            LiveIsoMethod(LiveIsoTarget.ISO_FILE, "ISO image file",
                          "New ISO saved where you choose, written later to USB drive or SD card with dd, Raspberry "
                          "Pi Imager or Etcher", iso_reason),
        ]

    def on_realize(self, widget):
        self.wizard_view.content_navigation_view = self.content_navigation_view
        self.wizard_view._window = self._window
        self.wizard_view.set_installation(self.installation_in_progress)

    def on_unrealize(self, widget):
        # Root access not passed to deployment is released with wizard.
        if self.connection and self.installation_in_progress is None:
            connection, self.connection = self.connection, None
            threading.Thread(target=connection.close, daemon=True).start()

    # Pages

    @Gtk.Template.Callback()
    def is_page_ready_to_continue(self, sender, page) -> bool:
        if page == self.method_page:
            method = self.method_selection_view.selected_item
            return method is not None and not method.unavailable
        if page == self.disk_page:
            return self.selected_disk is not None and self.connection is not None
        if page == self.system_page:
            return self._system_error() is None
        return True

    @Gtk.Template.Callback()
    def is_method_selectable(self, sender, method) -> bool:
        return not method.unavailable

    @Gtk.Template.Callback()
    def is_method_usable(self, sender, method) -> bool:
        return True

    def _target(self) -> LiveIsoTarget | None:
        method = self.method_selection_view.selected_item
        return method.target if method else None

    def _boot(self) -> LiveIsoBoot:
        return next((boot for boot, check in self.boot_checks.items() if check.get_active()), LiveIsoBoot.AS_BUILT)

    def on_method_changed(self, view):
        # Disk is selected only when writing to disk. ISO without Raspberry Pi kernel has only one way to boot.
        self.wizard_view.set_page_visible(self.disk_page, self._target() == LiveIsoTarget.LOCAL_DISK)
        self.wizard_view.set_page_visible(self.boot_page, self.raspberry_pi)
        self.wizard_view._refresh_buttons_state()

    def _on_boot_changed(self, check):
        if check is not None and not check.get_active():
            return
        self.raspberry_pi_group.set_visible(self._boot() == LiveIsoBoot.RASPBERRY_PI)

    # Disks

    @Gtk.Template.Callback()
    def on_find_disks_clicked(self, button):
        """Disks of this computer, listed as root (root access is kept while wizard is open)."""
        self.searching = True
        self.find_disks_button.set_sensitive(False)
        self._set_status(None)
        previous = self.connection
        architecture = self.project_directory.get_architecture() if self.project_directory else None
        def work():
            connection = previous if previous and previous.is_alive() else None
            error = None
            if connection is None:
                connection = LocalDiskConnection()
                error = connection.connect()
            machine = None
            if error is None:
                try:
                    machine = TargetMachine.load_local(connection, machine_architecture(architecture.value if architecture else None) or "",
                                                       TargetFirmware.OTHER)
                except Exception as e:
                    error = f"Failed to list disks: {e}"
                    connection.close()
            def done():
                self.searching = False
                self.find_disks_button.set_sensitive(True)
                self.find_disks_button.set_label("Find disks again")
                self.connection = connection if machine else None
                self.machine = machine
                self._set_status(error)
                self._load_disks()
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def _set_status(self, error: str | None):
        self.disk_status_label.set_visible(error is not None)
        self.disk_status_label.set_label(error or "")
        if error:
            self.disk_status_label.add_css_class("error")

    def _load_disks(self):
        for row in self.disk_rows:
            self.disks_group.remove(row)
        self.disk_rows = []
        self.selected_disk = None
        disks = self.machine.disks if self.machine else []
        size = os.path.getsize(self.build.artifact_path) if self.build and os.path.isfile(self.build.artifact_path or "") else 0
        group = None
        for disk in disks:
            too_small = disk.size < size * 1.1
            subtitle = disk.subtitle + (", too small for ISO" if too_small else "")
            row = Adw.ActionRow(title=GLib.markup_escape_text(disk.title), subtitle=GLib.markup_escape_text(subtitle))
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if group:
                check.set_group(group)
            group = group or check
            check.connect("toggled", self._on_disk_toggled, disk)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            row.set_sensitive(not disk.in_use and not too_small)
            self.disks_group.add(row)
            self.disk_rows.append(row)
        if not disks and self.machine:
            row = Adw.ActionRow(title="No disks found")
            self.disks_group.add(row)
            self.disk_rows.append(row)
        self.wizard_view._refresh_buttons_state()

    def _on_disk_toggled(self, check: Gtk.CheckButton, disk):
        if check.get_active():
            self.selected_disk = disk
            self.wizard_view._refresh_buttons_state()

    # Deploying

    def _raspberry_pi_settings(self) -> RaspberryPiBootSettings | None:
        if not self.raspberry_pi or self._boot() != LiveIsoBoot.RASPBERRY_PI:
            return None
        buffer = self.config_view.get_buffer()
        return RaspberryPiBootSettings(
            serial_console=self.serial_console_row.get_active(),
            extra_arguments=self.kernel_arguments_row.get_text().strip(),
            config_lines=buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False),
            older_models_firmware=self.older_models_row.get_active(),
        )

    @Gtk.Template.Callback()
    def begin_installation(self, view):
        target = self._target()
        if target == LiveIsoTarget.LOCAL_DISK:
            disk = self.selected_disk
            if disk is None or self.connection is None:
                return
            dialog = Adw.AlertDialog(heading="Erase disk?",
                                     body=f"All data on {disk.path} ({disk.subtitle}, {format_size(disk.size)}) of this "
                                          f"computer will be erased and {self.build.stage_name} will be written to it.")
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("deploy", "Erase and write")
            dialog.set_response_appearance("deploy", Adw.ResponseAppearance.DESTRUCTIVE)
        else:
            # Location of new ISO (save dialog asks before replacing existing file).
            name = os.path.basename(raspberry_pi_iso_path(self.build)) if self._raspberry_pi_settings() else \
                self.build.artifact.removesuffix(".iso") + "-custom.iso"
            downloads = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or os.path.expanduser("~")
            file_dialog = Gtk.FileDialog(title="Save ISO", initial_name=name, modal=True,
                                         initial_folder=Gio.File.new_for_path(downloads))
            file_dialog.save(self.get_root(), None, self._on_save_location_selected)
            return
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_confirm_response)
        dialog.present(self.get_root())

    def _on_save_location_selected(self, file_dialog, result):
        try:
            file = file_dialog.save_finish(result)
        except GLib.Error:
            return # Canceled.
        path = file.get_path() if file else None
        if path:
            self._start(output_path=path if path.endswith(".iso") else path + ".iso")

    def _on_confirm_response(self, dialog, response: str):
        if response == "deploy":
            self._start()

    def _start(self, output_path: str | None = None):
        target = self._target()
        installation = LiveIsoDeployment(
            self.project_directory, self.build, target,
            disk=self.selected_disk if target == LiveIsoTarget.LOCAL_DISK else None,
            disk_connection=self.connection if target == LiveIsoTarget.LOCAL_DISK else None,
            raspberry_pi=self._raspberry_pi_settings(),
            output_path=output_path,
            system=self._system_settings(),
        )
        if target != LiveIsoTarget.LOCAL_DISK and self.connection:
            threading.Thread(target=self.connection.close, daemon=True).start()
        self.connection = None # Owned by deployment now.
        self.installation_in_progress = installation
        installation.start()
        self.wizard_view.set_installation(installation)
