from __future__ import annotations
import os, shutil, sys, threading
from enum import Enum
from gi.repository import Gtk, Adw, GLib, Gio
import re
from .deploy_installation import DeployInstallation, DeploySystemSettings, local_mount_point, DeployRepository
from .user_row import UserRow
from .deploy_boot import Bootloader, Kernel, FIRMWARE_PACKAGE, StageContents, default_bootloader, bootloader_warnings
from .deploy_system import (
    NetworkService, NetworkSettings, LocalizationSettings, WifiSettings, default_network_service, local_localization, local_public_keys, local_wifi_ssid, local_user_identity
)
from .deploy_target import (
    TargetMachine, TargetFilesystem, PartitionPlan, PartitionSpec, PartitionType, GIB, MIB, format_size,
    architecture_supported, default_layout, PartitionTable, TargetFirmware, machine_architecture, MOUNTED_TYPES
)
from .ssh_connection import SSHConnection
from .local_disk_connection import LocalDiskConnection
from .combo_row_popup import fit_popup_to_items
from .deploy_image import image_supported_reason, image_connection, image_path, initial_image_size
from .deploy_target import default_image_layout
from .item_select_view import ItemSelectionViewEvent
from .disk_selection import populate_disk_rows

class DeployMethod(Enum):
    """Where build is deployed."""
    SSH = "ssh"
    LOCAL_DISK = "local-disk"
    IMAGE_FILE = "image-file"

    @property
    def title(self) -> str:
        return {DeployMethod.SSH: "Machine through SSH", DeployMethod.LOCAL_DISK: "Disk of this computer",
                DeployMethod.IMAGE_FILE: "Create disk image"}[self]

    @property
    def icon_name(self) -> str:
        return {DeployMethod.SSH: "network-wired-symbolic", DeployMethod.LOCAL_DISK: "drive-removable-media-symbolic",
                DeployMethod.IMAGE_FILE: "document-save-symbolic"}[self]

    @property
    def unavailable_reason(self) -> str | None:
        match self:
            case DeployMethod.SSH:
                return None if shutil.which("ssh") else "Not available: ssh command is not installed on this computer"
            case DeployMethod.LOCAL_DISK:
                # Installed system is configured in chroot, which needs Linux. Virtual machines used on macOS can't
                # access disks of this computer.
                return None if sys.platform.startswith("linux") else "Not available: needs Linux, virtual machines on this system can't access disks of this computer"
            case DeployMethod.IMAGE_FILE:
                return image_supported_reason(None)

    @property
    def unavailable(self) -> bool:
        return self.unavailable_reason is not None

    @property
    def details(self) -> str:
        description = {
            DeployMethod.SSH: "Other machine booted from Gentoo LiveCD, installed through SSH connection",
            DeployMethod.LOCAL_DISK: "SD card or external disk connected to this computer, booted later on other machine",
            DeployMethod.IMAGE_FILE: "Create an .img or compressed .img.xz file to write to an SD card or disk later with Raspberry Pi Imager or dd. "
                                     "It's sized to the system and grows to fill the disk at first start",
        }[self]
        return self.unavailable_reason or description

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/deploy/deploy_create_view.ui')
class DeployCreateView(Gtk.Box):
    """Wizard deploying stage build to machine booted from Gentoo LiveCD, or to disk of this computer (Linux only):
    connection, disk partitioning, system settings, users and bootloader."""
    __gtype_name__ = "DeployCreateView"

    wizard_view = Gtk.Template.Child()
    method_page = Gtk.Template.Child()
    method_selection_view = Gtk.Template.Child()
    connection_page = Gtk.Template.Child()
    local_group = Gtk.Template.Child()
    local_architecture_row = Gtk.Template.Child()
    target_firmware_row = Gtk.Template.Child()
    ssh_group = Gtk.Template.Child()
    host_row = Gtk.Template.Child()
    port_row = Gtk.Template.Child()
    user_row = Gtk.Template.Child()
    password_row = Gtk.Template.Child()
    connect_button = Gtk.Template.Child()
    machine_group = Gtk.Template.Child()
    machine_row = Gtk.Template.Child()
    connection_status_label = Gtk.Template.Child()
    disk_page = Gtk.Template.Child()
    disks_group = Gtk.Template.Child()
    partitions_group = Gtk.Template.Child()
    partitions_status_label = Gtk.Template.Child()
    table_row = Gtk.Template.Child()
    system_page = Gtk.Template.Child()
    hostname_row = Gtk.Template.Child()
    ssh_row = Gtk.Template.Child()
    reboot_row = Gtk.Template.Child()
    hostname_status_label = Gtk.Template.Child()
    localization_status_label = Gtk.Template.Child()
    network_status_label = Gtk.Template.Child()
    timezone_row = Gtk.Template.Child()
    locale_row = Gtk.Template.Child()
    keymap_row = Gtk.Template.Child()
    network_service_row = Gtk.Template.Child()
    network_mode_row = Gtk.Template.Child()
    interface_row = Gtk.Template.Child()
    address_row = Gtk.Template.Child()
    gateway_row = Gtk.Template.Child()
    dns_row = Gtk.Template.Child()
    ssh_keys_group = Gtk.Template.Child()
    users_page = Gtk.Template.Child()
    root_password_row = Gtk.Template.Child()
    root_password_confirm_row = Gtk.Template.Child()
    users_group = Gtk.Template.Child()
    root_status_label = Gtk.Template.Child()
    users_status_label = Gtk.Template.Child()
    boot_page = Gtk.Template.Child()
    stage_contents_row = Gtk.Template.Child()
    stage_contents_spinner = Gtk.Template.Child()
    bootloader_row = Gtk.Template.Child()
    kernel_row = Gtk.Template.Child()
    firmware_row = Gtk.Template.Child()
    boot_info_label = Gtk.Template.Child()

    def __init__(self, project_directory=None, build=None, installation_in_progress: DeployInstallation | None = None,
                 content_navigation_view: Adw.NavigationView | None = None):
        super().__init__()
        for row in (self.target_firmware_row, self.table_row, self.network_service_row, self.network_mode_row, self.kernel_row, self.bootloader_row):
            fit_popup_to_items(row)
        self._setup_wifi_rows()
        self.installation_in_progress = installation_in_progress
        self.content_navigation_view = content_navigation_view
        self.project_directory = project_directory
        self.build = build if build else installation_in_progress.build if installation_in_progress else None
        self._setup_repository_row()
        self.connection: SSHConnection | LocalDiskConnection | None = None
        self.machine: TargetMachine | None = None
        self.connecting = False
        self.selected_disk = None
        self.disk_rows: list[Adw.ActionRow] = []
        self.partition_rows: list[PartitionRow] = []
        self.layout: list[PartitionSpec] = []
        self.user_rows: list[UserRow] = []
        self.contents: StageContents | None = None # Read from archive in background.
        self.contents_error: str | None = None
        self.bootloader_options: list[Bootloader] = []
        self.kernel_options: list[Kernel] = []
        self.network_options: list[NetworkService] = []
        # Shown for image method, created before method is selected.
        self.compress_row = Adw.SwitchRow(title="Compress image", subtitle="Saved as .img.xz, Raspberry Pi Imager writes it directly", active=True)
        self.local_group.add(self.compress_row)
        self.method_selection_view.set_static_list(list(DeployMethod))
        self.method_selection_view.event_bus.subscribe(ItemSelectionViewEvent.ITEM_CHANGED, self.on_method_changed)
        self.on_method_changed(None) # First available method is selected when list is loaded.
        architecture = self.project_directory.get_architecture() if self.project_directory else None
        self.local_architecture = machine_architecture(architecture.value if architecture else None)
        self.local_architecture_row.set_subtitle(self.local_architecture or "Unknown architecture of build")
        self.firmware_options = TargetFirmware.options(self.local_architecture) if self.local_architecture else []
        self.target_firmware_row.set_model(Gtk.StringList.new([item.display_name() for item in self.firmware_options]))
        # Like warnings of stage settings: icon with details in tooltip.
        self.bootloader_warning_icon = Gtk.Image.new_from_icon_name("danger-triangle-svgrepo-com-symbolic")
        self.bootloader_warning_icon.add_css_class("warning")
        self.bootloader_warning_icon.set_visible(False)
        self.bootloader_row.add_suffix(self.bootloader_warning_icon)
        # Rows below update system page state when filled, widgets used there are created first.
        localization = local_localization()
        self.timezone_row.set_text(localization.timezone)
        self._setup_locales_editor()
        self.locales_view.get_buffer().set_text("\n".join(localization.locales))
        self.keymap_row.set_text(localization.keymap)
        self.key_checks: list[tuple[Gtk.CheckButton, str]] = []
        for key in local_public_keys():
            check = Gtk.CheckButton(active=False, valign=Gtk.Align.CENTER)
            row = Adw.ActionRow(title=GLib.markup_escape_text(key.title), subtitle=GLib.markup_escape_text(key.subtitle))
            row.add_prefix(check)
            row.set_activatable_widget(check)
            self.ssh_keys_group.add_row(row)
            self.key_checks.append((check, key.key))
        if not self.key_checks:
            self.ssh_keys_group.add_row(Adw.ActionRow(title="No SSH keys found in ~/.ssh"))
        self._update_ssh_options()
        self._update_network_options()
        if self.build and installation_in_progress is None:
            threading.Thread(target=self._scan_stage, daemon=True).start()
        if self.build:
            size = os.path.getsize(self.build.artifact_path) if self.build.artifact_path and os.path.isfile(self.build.artifact_path) else 0
            self.wizard_view.set_property("welcome_screen_description",
                f"Installs {self.build.stage_name} ({self.build.artifact}, {format_size(size)}) on disk of another machine. "
                "Boot that machine from Gentoo LiveCD and allow SSH connections to it" +
                (", or connect its disk (eg. SD card) to this computer" if not DeployMethod.LOCAL_DISK.unavailable else "") +
                ". Catalyst Lab partitions the disk, installs the stage and configures the system." +
                (" It can also create disk image file to write to SD cards or disks later." if not DeployMethod.IMAGE_FILE.unavailable else ""))
        self.connect("realize", self.on_realize)
        self.connect("unrealize", self.on_unrealize)

    def on_realize(self, widget):
        self.wizard_view.content_navigation_view = self.content_navigation_view
        self.wizard_view._window = self._window
        self.wizard_view.set_installation(self.installation_in_progress)

    def on_unrealize(self, widget):
        # Connection not passed to installation is closed with wizard.
        if self.connection and self.installation_in_progress is None:
            connection, self.connection = self.connection, None
            threading.Thread(target=connection.close, daemon=True).start()

    # Pages

    @Gtk.Template.Callback()
    def is_page_ready_to_continue(self, sender, page) -> bool:
        if page == self.method_page:
            method = self.method_selection_view.selected_item
            return method is not None and not method.unavailable
        if page == self.connection_page:
            return self.machine is not None
        if page == self.disk_page:
            plan = self._plan()
            return plan is not None and plan.error() is None
        if page == self.system_page:
            return self._system_error() is None
        if page == self.users_page:
            return self._users_error() is None
        if page == self.boot_page:
            return self.contents is not None or self.contents_error is not None
        return True

    # Connection

    # Deploy method

    @Gtk.Template.Callback()
    def is_method_selectable(self, sender, method) -> bool:
        return not method.unavailable

    @Gtk.Template.Callback()
    def is_method_usable(self, sender, method) -> bool:
        return True

    def _local(self) -> bool:
        """Disk of this computer, or image file (both installed here and booted on other machine)."""
        return self.method_selection_view.selected_item in (DeployMethod.LOCAL_DISK, DeployMethod.IMAGE_FILE)

    def _image(self) -> bool:
        return self.method_selection_view.selected_item == DeployMethod.IMAGE_FILE

    def on_method_changed(self, view):
        method = self.method_selection_view.selected_item
        if getattr(self, "_shown_method", None) == method and hasattr(self, "_shown_method"):
            return
        self._shown_method = method
        local = self._local()
        self.local_group.set_visible(local)
        self.local_group.set_title("Create disk image" if self._image() else "Disk of this computer")
        self.local_group.set_description(
            "Select the firmware of the machine that will boot this image. Installation creates the image in Temporary, then saves it to your chosen location."
            if self._image() else
            "Connect an SD card or external disk. Select the firmware of the machine that will boot from it. Root access is needed to write to the disk.")
        self.compress_row.set_visible(self._image())
        self.ssh_group.set_visible(not local)
        self.reboot_row.set_visible(not local)
        self.disks_group.set_visible(not self._image())
        # Machine or disks found with other method are not used.
        self.on_connection_changed(None)
        self.wizard_view._refresh_buttons_state()

    @Gtk.Template.Callback()
    def on_connection_changed(self, row):
        # Changed connection details need connecting again.
        if self.machine is not None and not self.connecting:
            self._set_machine(None)
            if self.connection:
                threading.Thread(target=self.connection.close, daemon=True).start()
                self.connection = None
        if self._image():
            self._prepare_image()
        self._update_connect_button()

    @Gtk.Template.Callback()
    def on_firmware_changed(self, row, param):
        # Firmware changes bootloaders and default layout, disks don't need to be listed again.
        if self.machine is None or not self.machine.local:
            return
        self.machine.firmware = self._firmware()
        self.machine.uefi = self.machine.firmware == TargetFirmware.UEFI
        self.machine_row.set_subtitle(GLib.markup_escape_text(self.machine.description))
        self._suggest_table()
        if self.selected_disk:
            self.layout = self._default_layout()
            self._load_partition_rows()
        self._update_boot_options()

    def _firmware(self) -> TargetFirmware:
        index = self.target_firmware_row.get_selected()
        return self.firmware_options[index] if 0 <= index < len(self.firmware_options) else TargetFirmware.OTHER

    def _update_connect_button(self):
        self.connect_button.set_visible(not self._image())
        if self._image():
            return
        if self._local():
            self.connect_button.set_sensitive(not self.connecting and self.local_architecture is not None)
            self.connect_button.set_label("Finding disks..." if self.connecting else "Find disks" if self.machine is None else "Find disks again")
            return
        self.connect_button.set_sensitive(not self.connecting and bool(self.host_row.get_text().strip())
                                          and self.port_row.get_text().strip().isdigit())
        self.connect_button.set_label("Connecting..." if self.connecting else "Connect" if self.machine is None else "Connect again")

    @Gtk.Template.Callback()
    def on_connect_clicked(self, sender):
        if self.connecting or not self.connect_button.get_sensitive():
            return
        if self._local():
            self._find_local_disks()
            return
        host = self.host_row.get_text().strip()
        port = int(self.port_row.get_text().strip() or 22)
        user = self.user_row.get_text().strip() or "root"
        password = self.password_row.get_text()
        local = False
        self.connecting = True
        self._set_status(None)
        self._update_connect_button()
        previous = self.connection
        def work():
            if previous:
                previous.close()
            connection = SSHConnection(host=host, port=port, user=user)
            machine, error = None, connection.connect(password)
            if error is None:
                try:
                    machine = TargetMachine.load(connection)
                except Exception as e:
                    error = f"Failed to read machine details: {e}"
                    connection.close()
            def done():
                self.connecting = False
                if self._local() != local:
                    # Method was changed while connecting.
                    if machine:
                        threading.Thread(target=connection.close, daemon=True).start()
                    self._update_connect_button()
                    return
                self.connection = connection if machine else None
                self._set_machine(machine)
                self._set_status(error)
                self._update_connect_button()
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def _find_local_disks(self):
        """Disks of this computer, listed as root (root access is kept while wizard is open)."""
        architecture, firmware = self.local_architecture, self._firmware()
        local = True
        self.connecting = True
        self._set_status(None)
        self._update_connect_button()
        previous = self.connection
        def work():
            connection = previous if isinstance(previous, LocalDiskConnection) and previous.is_alive() else None
            if connection is None:
                if previous:
                    previous.close()
                connection = LocalDiskConnection()
                error = connection.connect()
            else:
                error = None
            machine = None
            if error is None:
                try:
                    machine = TargetMachine.load_local(connection, architecture, firmware)
                except Exception as e:
                    error = f"Failed to list disks: {e}"
                    connection.close()
            def done():
                self.connecting = False
                if self._local() != local:
                    # Method was changed while connecting.
                    if machine:
                        threading.Thread(target=connection.close, daemon=True).start()
                    self._update_connect_button()
                    return
                self.connection = connection if machine else None
                self._set_machine(machine)
                self._set_status(error)
                self._update_connect_button()
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def _prepare_image(self):
        """Plan an image without starting a VM or requesting root access."""
        if self.local_architecture is None or self.build is None:
            self._set_machine(None)
            return
        self.connection = image_connection(self.project_directory)
        archive_size = os.path.getsize(self.build.artifact_path) if os.path.isfile(self.build.artifact_path or "") else 0
        self._set_machine(TargetMachine.for_image(
            self.connection, self.local_architecture, self._firmware(),
            image_path(self.build), initial_image_size(archive_size)))

    def _set_status(self, error: str | None):
        self.connection_status_label.set_visible(error is not None)
        self.connection_status_label.set_label(error or "")
        if error:
            self.connection_status_label.add_css_class("error")

    def _set_machine(self, machine: TargetMachine | None):
        self.machine = machine
        self.machine_group.set_visible(machine is not None and not machine.image)
        if machine and machine.image:
            self.machine_row.set_title(GLib.markup_escape_text(f"Image {os.path.basename(machine.disks[0].path)}"))
            creator = f" by {self.connection.host}" if self.connection.host.startswith("virtual machine") else ""
            self.machine_row.set_subtitle(GLib.markup_escape_text(f"{machine.description}\nCreated during installation in Temporary{creator}, saved where you choose when deploying"))
            if not self.hostname_row.get_text():
                self.hostname_row.set_text("gentoo")
            self._update_boot_options()
        elif machine and machine.local:
            available = sum(1 for disk in machine.disks if not disk.in_use)
            self.machine_row.set_title(GLib.markup_escape_text(f"{available} disk{'' if available == 1 else 's'} of this computer can be used"))
            self.machine_row.set_subtitle(GLib.markup_escape_text(machine.description))
            if not self.hostname_row.get_text():
                self.hostname_row.set_text("gentoo")
            self._update_boot_options()
        elif machine:
            self.machine_row.set_title(GLib.markup_escape_text(f"Connected to {machine.hostname or self.connection.host}"))
            subtitle = machine.description
            architecture = self.project_directory.get_architecture() if self.project_directory else None
            if architecture and architecture_supported(architecture.value, machine.architecture) is False:
                subtitle += f"\nWarning: build is for {architecture.value}, it won't run on this machine"
            self.machine_row.set_subtitle(GLib.markup_escape_text(subtitle))
            if not self.hostname_row.get_text():
                self.hostname_row.set_text("gentoo")
            # Static address suggested from LiveCD network.
            self.interface_row.set_text(machine.interface)
            self.address_row.set_text(machine.address)
            self.gateway_row.set_text(machine.gateway)
            self.dns_row.set_text(machine.dns)
            self._update_boot_options()
        self._suggest_table()
        self._load_disks()
        self.wizard_view._refresh_buttons_state()

    # Disk

    def _load_disks(self):
        disks = self.machine.disks if self.machine else []
        self.disk_rows = populate_disk_rows(
            self.disks_group, self.disk_rows, disks, self._on_disk_toggled, show_empty=bool(self.machine)
        )
        self.selected_disk = None
        self.layout = []
        if self.machine and self.machine.image:
            self.selected_disk = self.machine.disks[0]
            self.layout = self._default_layout()
        self._load_partition_rows()

    def _on_disk_toggled(self, check: Gtk.CheckButton, disk):
        if check.get_active():
            self.selected_disk = disk
            self.layout = self._default_layout()
            self._load_partition_rows()

    def _plan(self) -> PartitionPlan | None:
        if self.selected_disk is None or self.machine is None:
            return None
        return PartitionPlan(disk=self.selected_disk, uefi=self.machine.uefi, layout=self.layout, table=self._table(),
                             local=self.machine.local, raspberry_pi=self.machine.raspberry_pi,
                             image_path=self.selected_disk.path if self.machine.image else None,
                             compress_image=self.compress_row.get_active())

    def _table(self) -> PartitionTable:
        return list(PartitionTable)[self.table_row.get_selected()]

    def _default_layout(self) -> list[PartitionSpec]:
        if self.machine.image:
            return default_image_layout(self.machine.uefi, self.machine.architecture, self._table(), raspberry_pi=self.machine.raspberry_pi)
        return default_layout(self.selected_disk, self.machine.uefi, self.machine.memory, self.machine.architecture, self._table(),
                              raspberry_pi=self.machine.raspberry_pi)

    def _suggest_table(self):
        """Raspberry Pi models before 4 boot only from MBR."""
        if self.machine and self.machine.raspberry_pi:
            self.table_row.set_selected(list(PartitionTable).index(PartitionTable.MBR))

    @Gtk.Template.Callback()
    def on_table_changed(self, row, param):
        self._reset_layout()

    @Gtk.Template.Callback()
    def on_default_layout_clicked(self, button):
        self._reset_layout()

    def _reset_layout(self):
        if self.selected_disk and self.machine:
            self.layout = self._default_layout()
            self._load_partition_rows()

    # Partitions

    @Gtk.Template.Callback()
    def on_add_partition_clicked(self, button):
        # New partition uses remaining space when previous last one has fixed size.
        uses_rest = any(spec.size is None for spec in self.layout)
        self.layout.append(PartitionSpec(PartitionType.LINUX, 10 * GIB if uses_rest else None, TargetFilesystem.EXT4, ""))
        self._load_partition_rows(expand=len(self.layout) - 1)

    def _move_partition(self, spec: PartitionSpec, offset: int):
        index = self.layout.index(spec)
        target = index + offset
        if 0 <= target < len(self.layout):
            self.layout[index], self.layout[target] = self.layout[target], self.layout[index]
            self._load_partition_rows(expand=target)

    def _remove_partition(self, spec: PartitionSpec):
        self.layout.remove(spec)
        self._load_partition_rows()

    def _load_partition_rows(self, expand: int | None = None):
        """Rows are created again when partitions are added, removed or moved. Edited values update them."""
        for row in self.partition_rows:
            self.partitions_group.remove(row)
        self.partition_rows = []
        enabled = self.selected_disk is not None
        self.partitions_group.get_header_suffix().set_sensitive(enabled)
        self.table_row.set_sensitive(enabled)
        for index, spec in enumerate(self.layout):
            row = PartitionRow(spec, on_changed=self._update_partitions, on_move=self._move_partition, on_remove=self._remove_partition)
            row.set_expanded(index == expand)
            self.partitions_group.add(row)
            self.partition_rows.append(row)
        self._update_partitions()

    def _update_partitions(self):
        plan = self._plan()
        if plan is None:
            self.partitions_group.set_description("Select disk")
            self.partitions_status_label.set_visible(False)
            self.wizard_view._refresh_buttons_state()
            return
        for index, row in enumerate(self.partition_rows, start=1):
            # Space partition gets when it uses remaining space: space not used by other partitions.
            available = max(0, plan.remaining_size + (row.spec.size or 0))
            device = f"Partition {index}" if plan.image else plan.partition_device(index)
            row.update(device, plan.size_of(row.spec), available, first=index == 1, last=index == len(self.partition_rows), image=plan.image)
        free = plan.remaining_size if all(spec.size for spec in self.layout) else 0
        if plan.image:
            description = (f"{plan.table.display_name} partition table of image. Root partition is shrunk to the installed system "
                           "with some free space, and grows to fill the disk when the system starts first time")
            free = 0
        else:
            description = f"New {plan.table.display_name} partition table on {plan.disk.path}, {format_size(plan.usable_size)}"
        if free > 0:
            description += f", {format_size(free)} not used"
        self.partitions_group.set_description(description)
        messages = []
        if error := plan.error():
            messages.append(error)
        messages += [f"Warning: {warning}" for warning in plan.warnings(self.machine.architecture)]
        self.partitions_status_label.set_label("\n".join(messages))
        self.partitions_status_label.set_visible(bool(messages))
        self.wizard_view._refresh_buttons_state()

    # System

    @Gtk.Template.Callback()
    def on_system_changed(self, row, *args):
        # Rows of template report changes while view is created, before its editors exist.
        if not hasattr(self, "network_options") or not hasattr(self, "locales_view"):
            return
        network = self._network()
        static = network.service != NetworkService.NONE and network.static
        self.network_mode_row.set_visible(network.service != NetworkService.NONE)
        self.wifi_row.set_visible(network.service != NetworkService.NONE)
        if network.service != NetworkService.NONE:
            self.wifi_row.set_subtitle("NetworkManager connection" if network.service == NetworkService.NETWORK_MANAGER
                                       else "wpa_supplicant on wlan0, installed if stage doesn't have it")
        for widget in (self.interface_row, self.address_row, self.gateway_row, self.dns_row):
            widget.set_visible(static)
        # Errors are shown under groups of fields they refer to.
        _show_error(self.hostname_status_label, self._hostname_error())
        _show_error(self.localization_status_label, self._localization().error())
        _show_error(self.network_status_label, network.error() or (self._wifi().error() if self._wifi() else None))
        self.wizard_view._refresh_buttons_state()
        self.on_boot_changed() # Lists packages installed for selected network service.

    # Gentoo repository

    def _build_snapshot(self):
        """Snapshot used to build stage, when it's still in library."""
        from .repository import Repository
        name = (((getattr(self.build, "details", None) or {}).get("inputs") or {}).get("snapshot") or {}).get("file")
        return next((snapshot for snapshot in Repository.Snapshot.value if snapshot.filename == name), None) if name else None

    def _setup_repository_row(self):
        """Gentoo repository installed in system: none, snapshot used by build, or latest one (downloaded on machine)."""
        snapshot = self._build_snapshot() if self.build else None
        self.repository_options = [DeployRepository.NONE] + ([DeployRepository.BUILD_SNAPSHOT] if snapshot else []) + [DeployRepository.LATEST]
        labels = {DeployRepository.NONE: "Not installed",
                  DeployRepository.BUILD_SNAPSHOT: f"Snapshot of build ({snapshot.name if snapshot else ''})",
                  DeployRepository.LATEST: "Latest snapshot, downloaded on machine"}
        self.repository_row = Adw.ComboRow(title="Gentoo repository", model=Gtk.StringList.new([labels[option] for option in self.repository_options]))
        self.repository_row.set_subtitle(
            "Installed in /var/db/repos/gentoo, used by packages installed during deploy" if snapshot or not self.build else
            "Installed in /var/db/repos/gentoo. Snapshot of build isn't in library anymore")
        fit_popup_to_items(self.repository_row)
        self.firmware_row.get_ancestor(Adw.PreferencesGroup).add(self.repository_row)

    def _repository(self) -> tuple[DeployRepository, str | None]:
        index = self.repository_row.get_selected()
        option = self.repository_options[index] if 0 <= index < len(self.repository_options) else DeployRepository.NONE
        snapshot = self._build_snapshot() if option == DeployRepository.BUILD_SNAPSHOT else None
        return option, snapshot.file_path() if snapshot else None

    # Wi-Fi

    def _on_wifi_enabled(self, *_):
        if self.wifi_row.get_enable_expansion() and not getattr(self, "_wifi_detection_started", False):
            self._wifi_detection_started = True
            if not self.wifi_ssid_row.get_text():
                threading.Thread(target=self._suggest_wifi_name, daemon=True).start()
        self.on_system_changed(self.wifi_row)

    def _on_wifi_name_edited(self, *_):
        self._wifi_name_edited = True

    def _suggest_wifi_name(self):
        ssid = local_wifi_ssid(request_permission=True)
        def apply():
            if not ssid and sys.platform == "darwin":
                self.wifi_ssid_row.set_tooltip_text(
                    "If connected to Wi-Fi, allow Location access for Catalyst Lab in System Settings > Privacy & Security > Location Services, then reopen deploy. You can also enter the network name manually.")
            if ssid and not self._wifi_name_edited and not self.wifi_ssid_row.get_text():
                self.wifi_ssid_row.set_text(ssid)
            return False
        GLib.idle_add(apply)

    def _setup_wifi_rows(self):
        """Wi-Fi network connected by selected network service (NetworkManager connection, or wpa_supplicant)."""
        self.wifi_row = Adw.ExpanderRow(title="Wi-Fi", subtitle="Network connected automatically", show_enable_switch=True,
                                        enable_expansion=False)
        self.wifi_row.connect("notify::enable-expansion", self._on_wifi_enabled)
        self.wifi_ssid_row = Adw.EntryRow(title="Network name")
        self.wifi_ssid_row.connect("changed", self._on_wifi_name_edited)
        self._wifi_name_edited = False
        self.wifi_password_row = Adw.PasswordEntryRow(title="Password, empty for open network")
        self.wifi_hidden_row = Adw.SwitchRow(title="Hidden network", subtitle="Network doesn't broadcast its name")
        self.wifi_country_row = Adw.EntryRow(title="Country code, eg. PL (channels allowed in country)")
        for row in (self.wifi_ssid_row, self.wifi_password_row, self.wifi_country_row):
            row.connect("changed", self.on_system_changed)
        self.wifi_hidden_row.connect("notify::active", self.on_system_changed)
        for row in (self.wifi_ssid_row, self.wifi_password_row, self.wifi_hidden_row, self.wifi_country_row):
            self.wifi_row.add_row(row)
        self.network_service_row.get_ancestor(Adw.PreferencesGroup).add(self.wifi_row)

    def _wifi(self) -> WifiSettings | None:
        if not self.wifi_row.get_enable_expansion() or self._network().service == NetworkService.NONE:
            return None
        return WifiSettings(ssid=self.wifi_ssid_row.get_text(), password=self.wifi_password_row.get_text(),
                            hidden=self.wifi_hidden_row.get_active(), country=self.wifi_country_row.get_text().strip())

    # Users

    @Gtk.Template.Callback()
    def on_users_changed(self, *args):
        # Missing root password is not shown as error until something was typed.
        typed = bool(self.root_password_row.get_text() or self.root_password_confirm_row.get_text())
        _show_error(self.root_status_label, self._root_password_error() if typed else None)
        _show_error(self.users_status_label, self._user_accounts_error())
        self.wizard_view._refresh_buttons_state()

    @Gtk.Template.Callback()
    def on_add_user_clicked(self, button):
        row = UserRow(on_changed=self.on_users_changed, on_remove=self._remove_user,
                      available_groups=self.contents.groups if self.contents else None)
        self.users_group.add(row)
        self.user_rows.append(row)
        if not getattr(self, "_host_user_suggested", False):
            self._host_user_suggested = True
            username, full_name = local_user_identity()
            row.name_row.set_text(username)
            row.full_name_row.set_text(full_name)
        row.set_expanded(True)
        self.on_users_changed()

    def _remove_user(self, row: UserRow):
        self.users_group.remove(row)
        self.user_rows.remove(row)
        self.on_users_changed()

    def _users_error(self) -> str | None:
        return self._root_password_error() or self._user_accounts_error()

    def _root_password_error(self) -> str | None:
        if self.root_password_row.get_text() != self.root_password_confirm_row.get_text():
            return "Root passwords don't match"
        return None

    def _user_accounts_error(self) -> str | None:
        names = set()
        for row in self.user_rows:
            if error := row.error():
                return error
            name = row.user().name
            if name in names:
                return f"User {name} is added more than once"
            names.add(name)
        return None

    # Boot

    def _scan_stage(self):
        try:
            contents, error = StageContents.scan(self.build.artifact_path), None
        except Exception as e:
            contents, error = None, str(e)
        def done():
            self.contents, self.contents_error = contents, error
            for row in self.user_rows:
                row.set_available_groups(contents.groups if contents else None)
            self._update_ssh_options()
            self._update_network_options()
            self._update_boot_options()
            self.wizard_view._refresh_buttons_state()
        GLib.idle_add(done)

    def _update_ssh_options(self):
        contents = self.contents
        enabled = contents.ssh_enabled if contents else False
        installed = contents.ssh_installed if contents else False
        self.ssh_row.set_active(enabled)
        self.ssh_row.set_sensitive(installed and not enabled)
        self.ssh_row.set_subtitle(
            "Already enabled in the stage" if enabled else
            "Start SSH server on boot" if installed else
            "SSH server is not installed in the stage" if contents else
            "Could not check SSH server availability" if self.contents_error else
            "Checking SSH server in the stage…")

    def _update_boot_options(self):
        self.stage_contents_spinner.set_visible(self.contents is None and self.contents_error is None)
        if self.contents_error:
            self.stage_contents_row.set_subtitle(GLib.markup_escape_text(f"Failed to check stage: {self.contents_error}"))
        elif self.contents:
            self.stage_contents_row.set_subtitle(GLib.markup_escape_text(self.contents.summary))
        if self.machine is None:
            return
        contents = self.contents or StageContents()
        architecture, uefi, removable = self.machine.architecture, self.machine.uefi, self.machine.local
        self.bootloader_options = [item for item in Bootloader if item.supported(architecture, uefi, removable)]
        labels = []
        for item in self.bootloader_options:
            if not item.needs_package:
                labels.append(item.display_name)
            elif item in contents.bootloaders:
                labels.append(f"{item.display_name}, in stage")
            else:
                labels.append(f"{item.display_name}, will be installed")
        self.bootloader_row.set_model(Gtk.StringList.new(labels))
        raspberry_pi = self.machine.raspberry_pi
        self.bootloader_row.set_selected(self.bootloader_options.index(default_bootloader(contents, architecture, uefi, removable, raspberry_pi)))
        # Kernel from stage is used when it has one, otherwise prebuilt kernel is installed (Raspberry Pi kernel on Raspberry
        # Pi, distribution kernel elsewhere). Raspberry Pi kernel is offered only on architectures of Raspberry Pi.
        kernels = [Kernel.DISTRIBUTION_BINARY, Kernel.DISTRIBUTION]
        if Kernel.RASPBERRY_PI.supported(architecture):
            kernels.insert(0 if raspberry_pi else 2, Kernel.RASPBERRY_PI)
        self.kernel_options = ([Kernel.STAGE] if contents.kernels else []) + kernels + [Kernel.NONE]
        self.kernel_row.set_model(Gtk.StringList.new([
            f"{item.display_name} ({', '.join(contents.kernels)})" if item == Kernel.STAGE else item.display_name
            for item in self.kernel_options
        ]))
        self.kernel_row.set_selected(0)
        # Firmware is offered only when stage doesn't have it. Raspberry Pi kernel doesn't need linux-firmware.
        self.firmware_row.set_visible(not contents.firmware)
        self.firmware_row.set_active(not contents.kernels and not contents.firmware and not raspberry_pi and not contents.firmware_conflict)
        # Firmware package of stage that blocks linux-firmware (eg. Raspberry Pi Wi-Fi firmware), it can't be installed.
        self.firmware_row.set_sensitive(not contents.firmware_conflict)
        if not hasattr(self, "_firmware_subtitle"):
            self._firmware_subtitle = self.firmware_row.get_subtitle()
        self.firmware_row.set_subtitle(GLib.markup_escape_text(
            f"Not available: stage has {contents.firmware_conflict}, which can't be installed together with it"
            if contents.firmware_conflict else self._firmware_subtitle or ""))
        self.on_boot_changed()

    def _install_firmware(self) -> bool:
        return (self.firmware_row.get_active() and not (self.contents and self.contents.firmware)
                and not (self.contents and self.contents.firmware_conflict))

    def _selected_kernel(self) -> Kernel:
        index = self.kernel_row.get_selected()
        return self.kernel_options[index] if 0 <= index < len(self.kernel_options) else Kernel.NONE

    def _selected_bootloader(self) -> Bootloader:
        index = self.bootloader_row.get_selected()
        return self.bootloader_options[index] if 0 <= index < len(self.bootloader_options) else Bootloader.NONE

    @Gtk.Template.Callback()
    def on_boot_changed(self, *args):
        if not hasattr(self, "bootloader_options"):
            return
        contents = self.contents or StageContents()
        bootloader = self._selected_bootloader()
        kernel = self._selected_kernel()
        has_kernel = bool(kernel.package) or (kernel == Kernel.STAGE and bool(contents.kernels))
        self._update_bootloader_warnings(bootloader, has_kernel)
        installs = []
        if bootloader.needs_package and bootloader not in contents.bootloaders:
            installs.append(bootloader.package(contents.init))
        network = self._network().service
        if network.package and network not in contents.network_services:
            installs.append(network.package)
        if self._install_firmware():
            installs.append(FIRMWARE_PACKAGE)
        if kernel.package:
            installs.append(kernel.package)
        notes = []
        local = self.machine is not None and self.machine.local
        if installs:
            notes.append(f"{', '.join(installs)} will be installed with emerge {'in installed system on this computer' if local else 'on the machine'}, it needs internet connection. "
                         "Gentoo repository is downloaded when stage doesn't contain it, packages are compiled unless stage has binary packages repository configured.")
        if kernel.package:
            notes.append("Installed kernel gets initramfs generated by dracut" + (", with drivers for any machine." if local else "."))
        if local and not runs_natively(self.machine.architecture):
            notes.append(f"Programs of installed system run on this computer with QEMU user emulation of {self.machine.architecture}, "
                         "it needs to be installed and configuring the system is slower.")
        if kernel == Kernel.DISTRIBUTION:
            notes.append("Compiling kernel can take an hour or more.")
        # Missing kernel is shown as bootloader warning.
        if bootloader == Bootloader.NONE:
            notes.append("System won't boot until bootloader is installed manually.")
        self.boot_info_label.set_label(" ".join(notes))
        self.boot_info_label.set_visible(bool(notes))

    def _update_bootloader_warnings(self, bootloader: Bootloader, has_kernel: bool):
        warnings = bootloader_warnings(bootloader, self.machine.uefi, self.machine.local, has_kernel) if self.machine else []
        self.bootloader_warning_icon.set_visible(bool(warnings))
        self.bootloader_warning_icon.set_tooltip_text("\n\n".join(warning.details for warning in warnings) or None)
        self.bootloader_row.set_subtitle(GLib.markup_escape_text(", ".join(warning.summary for warning in warnings)))

    def _system_error(self) -> str | None:
        wifi = self._wifi()
        return self._hostname_error() or self._localization().error() or self._network().error() or (wifi.error() if wifi else None)

    def _hostname_error(self) -> str | None:
        hostname = self.hostname_row.get_text().strip()
        if hostname and not all(character.isalnum() or character in "-." for character in hostname):
            return "Hostname can contain only letters, digits, dots and hyphens"
        return None

    def _setup_locales_editor(self):
        """Locales are edited one per line in expandable row, like list values in stage settings. Row subtitle shows
        them when collapsed."""
        self.locales_view = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False,
                                         top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        self.locales_view.set_size_request(-1, 96)
        self.locales_view.get_buffer().connect("changed", self._on_locales_changed)
        frame = Gtk.Frame(child=self.locales_view)
        hint_label = Gtk.Label(label="One locale per line, first one is system language.", halign=Gtk.Align.START, wrap=True)
        hint_label.add_css_class("dimmed")
        hint_label.add_css_class("caption")
        editor_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=12, margin_bottom=12,
                             margin_start=12, margin_end=12)
        editor_box.append(frame)
        editor_box.append(hint_label)
        editor_row = Gtk.ListBoxRow(activatable=False, selectable=False, child=editor_box)
        self.locale_row.add_row(editor_row)

    def _locales(self) -> list[str]:
        # Commas are accepted too, as separators.
        buffer = self.locales_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        return [locale for locale in re.split(r"[,\s]+", text) if locale]

    def _on_locales_changed(self, buffer):
        locales = self._locales()
        self.locale_row.set_subtitle(GLib.markup_escape_text(", ".join(locales) or "None"))
        self.on_system_changed(None)

    def _localization(self) -> LocalizationSettings:
        return LocalizationSettings(timezone=self.timezone_row.get_text().strip(),
                                    locales=self._locales(),
                                    keymap=self.keymap_row.get_text().strip())

    def _network(self) -> NetworkSettings:
        index = self.network_service_row.get_selected()
        service = self.network_options[index] if 0 <= index < len(self.network_options) else NetworkService.NONE
        return NetworkSettings(
            service=service, static=self.network_mode_row.get_selected() == 1,
            interface=self.interface_row.get_text().strip(), address=self.address_row.get_text().strip(),
            gateway=self.gateway_row.get_text().strip(), dns=self.dns_row.get_text().strip(),
        )

    def _update_network_options(self):
        """Services found in stage, or dhcpcd installed from repository when stage has none."""
        contents = self.contents or StageContents()
        selected = self._network().service if self.network_options else None
        self.network_options = [service for service in NetworkService if service.supported(contents.init)
                                and (service in contents.network_services or service in (NetworkService.DHCPCD, NetworkService.NONE))]
        labels = [service.display_name if service == NetworkService.NONE else
                  f"{service.display_name}, {'in stage' if service in contents.network_services else 'will be installed'}"
                  for service in self.network_options]
        self.network_service_row.set_model(Gtk.StringList.new(labels))
        default = selected if selected in self.network_options and self.contents is None else default_network_service(contents.network_services, contents.init)
        self.network_service_row.set_selected(self.network_options.index(default))
        self.on_system_changed(None)

    # Installation

    @Gtk.Template.Callback()
    def begin_installation(self, view):
        plan = self._plan()
        if plan is None or self.connection is None:
            return
        owner = "this computer" if self.machine.local else self.connection.host
        if plan.image:
            # Location of image (save dialog asks before replacing existing file). Working image is created in Temporary,
            # where virtual machine can write, and moved there.
            name = os.path.basename(plan.image_path) + (".xz" if plan.compress_image else "")
            downloads = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or os.path.expanduser("~")
            file_dialog = Gtk.FileDialog(title="Save image", initial_name=name, modal=True,
                                         initial_folder=Gio.File.new_for_path(downloads))
            file_dialog.save(self.get_root(), None, self._on_image_location_selected, plan)
            return
        else:
            dialog = Adw.AlertDialog(
                heading="Erase disk?",
                body=f"All data on {plan.disk.path} ({plan.disk.subtitle}, {format_size(plan.disk.size)}) of {owner} will be erased and {self.build.stage_name} will be installed on it."
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("deploy", "Erase and deploy")
            dialog.set_response_appearance("deploy", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_confirm_response, plan)
        dialog.present(self.get_root())

    def _on_image_location_selected(self, file_dialog, result, plan: PartitionPlan):
        try:
            file = file_dialog.save_finish(result)
        except GLib.Error:
            return # Canceled.
        if file and file.get_path():
            self._on_confirm_response(None, "deploy", plan, output_path=file.get_path())

    def _on_confirm_response(self, dialog, response: str, plan: PartitionPlan, output_path: str | None = None):
        if response != "deploy":
            return
        if self.machine.local:
            plan.root = local_mount_point()
        settings = DeploySystemSettings(
            hostname=self.hostname_row.get_text().strip() or None,
            root_password=self.root_password_row.get_text() or None,
            users=[row.user() for row in self.user_rows],
            enable_ssh=self.ssh_row.get_active(),
            ssh_keys=[key for check, key in self.key_checks if check.get_active()],
            network=self._network(),
            localization=self._localization(),
            bootloader=self._selected_bootloader(),
            kernel=self._selected_kernel(),
            install_firmware=self._install_firmware(),
            reboot=self.reboot_row.get_active() and not self.machine.local,
            wifi=self._wifi(),
            repository=self._repository()[0],
            repository_snapshot=self._repository()[1],
        )
        installation = DeployInstallation(
            build=self.build, project_name=self.project_directory.name if self.project_directory else "",
            connection=self.connection, machine=self.machine, plan=plan, settings=settings,
            contents=self.contents or StageContents(),
        )
        installation.project_id = self.project_directory.id if self.project_directory else None
        installation.output_path = output_path
        self.installation_in_progress = installation
        installation.start()
        self.wizard_view.set_installation(installation)


def _show_error(label: Gtk.Label, error: str | None):
    label.set_visible(error is not None)
    label.set_label(error or "")


class PartitionRow(Adw.ExpanderRow):
    """Partition of layout: type, filesystem, mount point and size."""

    _TYPES = list(PartitionType)
    _FILESYSTEMS = [TargetFilesystem.EXT4, TargetFilesystem.XFS, TargetFilesystem.BTRFS, TargetFilesystem.VFAT]

    def __init__(self, spec: PartitionSpec, on_changed, on_move, on_remove):
        super().__init__()
        self.spec = spec
        self.on_changed = on_changed
        self._loading = True
        self.type_row = fit_popup_to_items(Adw.ComboRow(title="Type", model=Gtk.StringList.new([item.display_name for item in self._TYPES])))
        self.type_row.set_selected(self._TYPES.index(spec.type))
        self.filesystem_row = fit_popup_to_items(Adw.ComboRow(title="Filesystem", model=Gtk.StringList.new([item.value for item in self._FILESYSTEMS])))
        self.filesystem_row.set_selected(self._FILESYSTEMS.index(spec.filesystem or TargetFilesystem.EXT4))
        self.mount_row = Adw.EntryRow(title="Mount point", text=spec.mount_point or "")
        self.rest_row = Adw.SwitchRow(title="Use remaining space", active=spec.size is None)
        self.size_row = Adw.SpinRow(title="Size", subtitle="GiB", digits=1,
                                    adjustment=Gtk.Adjustment(lower=0.1, upper=1024 * 64, step_increment=1, page_increment=10))
        self.size_row.set_value(round((spec.size or 10 * GIB) / GIB, 1))
        for row in (self.type_row, self.filesystem_row, self.mount_row, self.rest_row, self.size_row):
            self.add_row(row)
        self.type_row.connect("notify::selected", self._on_edited)
        self.filesystem_row.connect("notify::selected", self._on_edited)
        self.mount_row.connect("changed", self._on_edited)
        self.rest_row.connect("notify::active", self._on_edited)
        self.size_row.connect("notify::value", self._on_edited)
        buttons = Gtk.Box(spacing=0, valign=Gtk.Align.CENTER)
        self.up_button = self._button("go-up-symbolic", "Move up", lambda button: on_move(spec, -1))
        self.down_button = self._button("go-down-symbolic", "Move down", lambda button: on_move(spec, 1))
        for button in (self.up_button, self.down_button, self._button("user-trash-symbolic", "Remove partition", lambda button: on_remove(spec))):
            buttons.append(button)
        self.add_suffix(buttons)
        self._loading = False
        self._update_visibility()

    @staticmethod
    def _button(icon: str, tooltip: str, handler) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
        button.add_css_class("flat")
        button.connect("clicked", handler)
        return button

    def _on_edited(self, *args):
        if self._loading:
            return
        previous_type = self.spec.type
        self.spec.type = self._TYPES[self.type_row.get_selected()]
        if self.spec.type != previous_type:
            # Suggested values for new type.
            self._loading = True
            if self.spec.type == PartitionType.EFI:
                self.mount_row.set_text("/efi")
                self.rest_row.set_active(False)
                self.size_row.set_value(1)
            elif self.spec.type == PartitionType.FIRMWARE:
                self.mount_row.set_text("/boot")
                self.rest_row.set_active(False)
                self.size_row.set_value(0.5)
            elif self.spec.type == PartitionType.LINUX and self.mount_row.get_text() == "/efi":
                self.mount_row.set_text("")
            self._loading = False
        self.spec.filesystem = self._FILESYSTEMS[self.filesystem_row.get_selected()]
        self.spec.mount_point = self.mount_row.get_text().strip() or None
        if self.spec.type == PartitionType.BIOS_BOOT:
            self.spec.size = 1 * MIB
        else:
            self.spec.size = None if self.rest_row.get_active() else int(self.size_row.get_value() * GIB) // MIB * MIB
        self._update_visibility()
        self.on_changed()

    def _update_visibility(self):
        partition_type = self.spec.type
        self.filesystem_row.set_visible(partition_type == PartitionType.LINUX)
        self.mount_row.set_visible(partition_type in MOUNTED_TYPES)
        self.rest_row.set_visible(partition_type in (PartitionType.LINUX, PartitionType.SWAP))
        self.size_row.set_visible(partition_type != PartitionType.BIOS_BOOT and not (self.rest_row.get_visible() and self.rest_row.get_active()))

    def update(self, device: str, size: int, available: int, first: bool, last: bool, image: bool = False):
        spec = self.spec
        self.rest_row.set_subtitle(("Sized to contents, grows to fill disk" if image else f"Uses {format_size(size)}") if spec.size is None
                                   else f"Would use {format_size(available)}")
        purpose = spec.mount_point if spec.type in MOUNTED_TYPES and spec.mount_point else spec.type.display_name
        self.set_title(GLib.markup_escape_text(f"{device} · {purpose}"))
        details = ["sized to contents, grows to fill disk" if image and spec.size is None
                   else format_size(size) + (" (remaining space)" if spec.size is None else "")]
        if spec.type == PartitionType.LINUX:
            details.append((spec.filesystem or TargetFilesystem.EXT4).value)
        elif spec.type == PartitionType.EFI:
            details.append("EFI system, vfat")
        elif spec.type == PartitionType.FIRMWARE:
            details.append("Firmware boot, vfat")
        else:
            details.append(spec.type.display_name)
        self.set_subtitle(GLib.markup_escape_text(", ".join(details)))
        self.up_button.set_sensitive(not first)
        self.down_button.set_sensitive(not last)


def runs_natively(architecture: str) -> bool:
    """Programs of architecture (uname -m) run on this computer without emulation."""
    host = os.uname().machine
    return architecture == host or (host == "x86_64" and architecture in ("i686", "i586", "i486"))
