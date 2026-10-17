from __future__ import annotations
import json
import os
import threading
from gi.repository import Gtk, Adw, GLib
from dataclasses import replace
from .deploy_system import local_user_identity
from .user_row import UserRow
from .kernel_package_row import KernelPackageRow
from .wizard_view import WizardView
from .qemu_run import (QemuRun, QemuBuildKind, qemu_support, create_stage_image_directory,
                       kernel_build_toolsets, read_host_resolv_conf)
from .qemu_run_view import QemuImagePreparation, QemuRunView
from .multistage_process import MultiStageProcessState
from .apple_virtualization import apple_support
from .repository import Repository


class QemuLaunchWizard(Gtk.Box):
    """Select an available VM backend and resources before launching a build."""

    def __init__(self, project_directory, build):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.project_directory = project_directory
        self.build = build
        self._launch_started = False
        self.auto_close_on_finish = True
        self.support = qemu_support(project_directory, build)
        self.apple = apple_support(project_directory, build)
        self.kernel_required = self.support.kind == QemuBuildKind.STAGE and self.support.kernel is None
        self.backends = []
        if self.apple.available:
            self.backends.append(("Apple Virtualization", "apple"))
        if self.support.available:
            self.backends.append(("QEMU", "qemu"))
        self.wizard_view = WizardView()
        self.wizard_view._window = None
        self.wizard_view.show_welcome_screen = True
        self.wizard_view.welcome_screen_icon_name = "computer-symbolic"
        self.wizard_view.welcome_screen_title = "Run in Virtual Machine"
        artifact_name = os.path.basename(build.artifact_path or build.artifact or "the selected build")
        self.wizard_view.welcome_screen_description = (
            f"Boot {build.stage_name} ({artifact_name}) in an isolated virtual machine. "
            "Choose the VM resources and, for a stage, optionally build a kernel or configure login accounts. "
            "Changes made while the VM runs are temporary.")
        # Install the welcome page before adding setup pages so the carousel's
        # initial position remains the welcome screen after pages are appended.
        self.wizard_view._setup_welcome_page()
        self.wizard_view.install_button_label = "Start virtual machine"
        self.wizard_view.connect("is-page-ready-to-continue", self._page_ready)
        self.wizard_view.connect("begin-installation", self._start)
        self.append(self.wizard_view)
        self.connect("realize", self._on_realize)

        def add_page(title, description):
            page = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
            page.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                              margin_start=24, margin_end=24, margin_top=24, margin_bottom=24)
            group = Adw.PreferencesGroup(title=title, description=description) if title else None
            if group:
                content.append(group)
            page.set_child(content)
            self.wizard_view.carousel.append(page)
            self.wizard_view.pages.append(page)
            self.wizard_view._all_pages.append(page)
            return page, content, group

        self.stage_page, stage_content, stage_group = add_page(
            "Selected stage", "Review the build to boot in a virtual machine.")
        settings = Repository.Settings.value
        self.vm_page, _vm_content, settings_group = add_page(
            "Virtual machine", "Choose the backend and resources for this run.")
        self.kernel_page, _kernel_content, kernel_group = add_page(
            "Kernel", "Use the stage kernel or build a kernel package for this run.")
        self.accounts_page, accounts_content, _accounts_group = add_page(None, None)
        is_stage = self.support.kind == QemuBuildKind.STAGE
        self.wizard_view.set_page_visible(self.kernel_page, is_stage)
        self.wizard_view.set_page_visible(self.accounts_page, is_stage)

        warning_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        warning_list.add_css_class("boxed-list")
        warning = Adw.ActionRow(title="Run in VM is in testing",
                                subtitle="VM builds are still being tested and may fail or behave unexpectedly.")
        warning.add_css_class("warning")
        warning.set_activatable(False)
        warning_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        warning_icon.add_css_class("warning")
        warning.add_prefix(warning_icon)
        warning_list.append(warning)
        # Keep the testing notice with the selected artifact, before run configuration.
        stage_content.prepend(warning_list)
        architecture = project_directory.get_architecture()
        architecture_name = architecture.name if architecture else "Unknown"
        kind = "Live CD ISO" if self.support.kind == QemuBuildKind.ISO else "Stage archive"
        artifact = os.path.basename(build.artifact_path or build.artifact or "Unknown")
        for title, value in (("Project", project_directory.name), ("Stage", build.stage_name),
                             ("Architecture", architecture_name), ("Build type", kind), ("Artifact", artifact)):
            stage_group.add(Adw.ActionRow(title=title, subtitle=value))
        self.backend_row = Adw.ComboRow(title="Virtualization backend",
            model=Gtk.StringList.new([name for name, _ in self.backends] or ["No backend available"]))
        self.backend_row.set_sensitive(len(self.backends) > 1)
        settings_group.add(self.backend_row)
        self.cpus_row = Adw.SpinRow(title="CPUs", adjustment=Gtk.Adjustment(
            lower=1, upper=64, step_increment=1, value=settings.qemu_cpus))
        self.memory_row = Adw.SpinRow(title="Memory (GiB)", adjustment=Gtk.Adjustment(
            lower=1, upper=256, step_increment=1, value=settings.qemu_memory_gib))
        settings_group.add(self.cpus_row)
        settings_group.add(self.memory_row)
        self.host_resolv_conf = read_host_resolv_conf()
        self.host_dns_row = Adw.SwitchRow(
            title="Set guest /etc/resolv.conf from host",
            subtitle=("Copies this computer's DNS settings into this run's writable stage image."
                      if self.host_resolv_conf else "No DNS nameserver was found in the host's /etc/resolv.conf."))
        self.host_dns_row.set_active(bool(self.host_resolv_conf))
        self.host_dns_row.set_visible(is_stage)
        self.host_dns_row.set_sensitive(bool(self.host_resolv_conf))
        settings_group.add(self.host_dns_row)
        self.custom_kernel_row = Adw.SwitchRow(
            title="Build a kernel package",
            subtitle="Build it in a Gentoo toolset using the snapshot and stage overlays. The stage does not need Portage.")
        self.custom_kernel_row.set_visible(self.support.kind == QemuBuildKind.STAGE)
        kernel_group.add(self.custom_kernel_row)
        self.kernel_build_toolsets = kernel_build_toolsets(project_directory)
        self.kernel_toolset_row = Adw.ComboRow(
            title="Toolset", subtitle="Choose a free toolset for this kernel build",
            model=Gtk.StringList.new([toolset.name for toolset in self.kernel_build_toolsets]
                                     or ["No free Gentoo toolsets"]))
        self.kernel_toolset_row.set_visible(False)
        self.kernel_toolset_row.set_sensitive(bool(self.kernel_build_toolsets))
        kernel_group.add(self.kernel_toolset_row)
        self.snapshots = [snapshot for snapshot in Repository.Snapshot.value
                          if os.path.isfile(snapshot.file_path())]
        self.kernel_snapshot_row = Adw.ComboRow(
            title="Gentoo snapshot",
            model=Gtk.StringList.new([self._snapshot_label(snapshot) for snapshot in self.snapshots]
                                     or ["No snapshots available"]))
        project_snapshot = project_directory.get_snapshot()
        selected_snapshot = next((index for index, snapshot in enumerate(self.snapshots)
                                  if project_snapshot and snapshot.filename == project_snapshot.filename), 0)
        self.kernel_snapshot_row.set_selected(selected_snapshot)
        self.kernel_snapshot_row.set_sensitive(bool(self.snapshots))
        self.kernel_snapshot_row.set_visible(False)
        kernel_group.add(self.kernel_snapshot_row)
        self._kernel_package_load_generation = 0
        self._kernel_packages_loading = False
        self.kernel_packages = []
        self.kernel_package_row = KernelPackageRow(
            "Kernel package", self._custom_kernel_changed, custom_title="Custom package atom")
        self.kernel_package_row.set_model(Gtk.StringList.new(["Loading kernel packages…"]))
        self.kernel_package_row.set_sensitive(False)
        kernel_group.add(self.kernel_package_row)
        kernel_group.add(self.kernel_package_row.custom_row)
        self.kernel_package_error = Adw.ActionRow(title="Enter a package atom such as sys-kernel/gentoo-kernel, optionally qualified with ::repository.")
        self.kernel_package_error.add_css_class("warning")
        self.kernel_package_error.set_visible(False)
        kernel_group.add(self.kernel_package_error)
        self.kernel_cache_row = Adw.ActionRow(title="Prepared kernel cache", subtitle="Select a kernel package to check its cache.")
        self.kernel_cache_row.set_visible(False)
        kernel_group.add(self.kernel_cache_row)
        self._kernel_cache_generation = 0
        self._kernel_cache_timeout = 0

        self.user_rows = []
        self._host_user_suggested = False
        self.users_group = Adw.PreferencesGroup(
            title="Users", description="Groups are selected from stage options; missing groups are skipped.")
        add_user_button = Gtk.Button(child=Adw.ButtonContent(label="Add user", icon_name="list-add-symbolic"))
        add_user_button.add_css_class("flat")
        add_user_button.connect("clicked", self._add_user)
        self.users_group.set_header_suffix(add_user_button)

        root_group = Adw.PreferencesGroup(title="Root", description="Optional. Leave empty to keep the stage's existing root password.")
        self.root_password_entry = Adw.PasswordEntryRow(title="Root password")
        self.root_password_confirm = Adw.PasswordEntryRow(title="Confirm root password")
        for row in (self.root_password_entry, self.root_password_confirm):
            root_group.add(row)
            row.connect("changed", self._login_settings_changed)
        accounts_content.append(root_group)
        accounts_content.append(self.users_group)
        self.login_settings_error = Adw.ActionRow(title="")
        self.login_settings_error.add_css_class("warning")
        self.login_settings_error.set_visible(False)
        root_group.add(self.login_settings_error)

        unavailable = []
        if not self.support.available:
            unavailable.append(f"QEMU: {self.support.reason}")
        import sys
        if sys.platform == "darwin" and not self.apple.available:
            unavailable.append(f"Apple Virtualization: {self.apple.reason}")
        if unavailable:
            settings_group.add(Adw.ActionRow(title="Backend status", subtitle="\n".join(unavailable)))
        self.backend_row.connect("notify::selected", self._backend_changed)
        self.custom_kernel_row.connect("notify::active", self._custom_kernel_changed)
        self.kernel_toolset_row.connect("notify::selected", self._custom_kernel_changed)
        self.kernel_snapshot_row.connect("notify::selected", self._kernel_snapshot_changed)
        self.kernel_package_row.connect("notify::selected", self._custom_kernel_changed)
        self.kernel_package_row.custom_row.connect("changed", self._custom_kernel_changed)
        self.kernel_package_row.custom_row.connect("apply", self._custom_kernel_changed)
        self._custom_kernel_reason = self._custom_kernel_unsupported_reason()
        self.custom_kernel_row.set_sensitive(self._custom_kernel_reason is None and not self.kernel_required)
        if self.kernel_required:
            self.custom_kernel_row.set_active(True)
            subtitle = "This stage has no boot kernel. Build a custom kernel package to run it."
            if self._custom_kernel_reason:
                subtitle += " " + self._custom_kernel_reason
            self.custom_kernel_row.set_subtitle(subtitle)
        elif self._custom_kernel_reason:
            self.custom_kernel_row.set_subtitle(self._custom_kernel_reason)
        self._backend_changed()
        self._custom_kernel_changed()
        self.wizard_view._refresh_buttons_state()
        if self.custom_kernel_row.get_visible() and self.snapshots:
            self._load_kernel_packages()

    def header_bar_end_widgets(self) -> list[Gtk.Widget]:
        return []

    def _page_ready(self, _wizard, page) -> bool:
        if page == self.vm_page:
            return bool(self.backends) and not any(run.is_running for run in QemuRun.running_runs)
        if page == self.kernel_page:
            toolset = self._selected_kernel_toolset()
            return (not self.kernel_required or self._custom_kernel_reason is None) and (
                not self.kernel_required or self.custom_kernel_row.get_active()) and (
                not self.custom_kernel_row.get_active() or (toolset is not None and not toolset.busy_reason)) and (
                not self.custom_kernel_row.get_active() or (not self._kernel_packages_loading
                and (not self._is_custom_kernel_selected() or self._valid_custom_kernel())))
        if page == self.accounts_page:
            return self._account_settings_error() is None
        return True

    def _add_user(self, *_):
        row = UserRow(self._login_settings_changed, self._remove_user)
        self.users_group.add(row)
        self.user_rows.append(row)
        if not self._host_user_suggested:
            self._host_user_suggested = True
            username, full_name = local_user_identity()
            row.name_row.set_text(username)
            row.full_name_row.set_text(full_name)
        row.set_expanded(True)
        self._login_settings_changed()

    def _remove_user(self, row):
        self.users_group.remove(row)
        self.user_rows.remove(row)
        self._login_settings_changed()

    def _login_settings_changed(self, *_):
        for row in self.user_rows:
            user = row.user()
            error = row.error()
            if error is None and not user.password:
                error = f"Set a password for {user.name} to enable login in the VM."
            if error is None and sum(other.user().name == user.name for other in self.user_rows) > 1:
                error = f"User {user.name} is added more than once"
            row.set_error(error)
        error = self._login_settings_error()
        self.login_settings_error.set_title(error or "")
        self.login_settings_error.set_visible(error is not None)
        self.wizard_view._refresh_buttons_state()

    def _login_settings_error(self):
        if self.support.kind != QemuBuildKind.STAGE:
            return None
        root_password = self.root_password_entry.get_text()
        root_confirmation = self.root_password_confirm.get_text()
        if root_password != root_confirmation:
            return "Root passwords don't match"
        return None

    def _account_settings_error(self):
        for row in self.user_rows:
            user = row.user()
            if error := row.error():
                return error
            if not user.password:
                return f"Set a password for {user.name} to enable login in the VM."
            if sum(other.user().name == user.name for other in self.user_rows) > 1:
                return f"User {user.name} is added more than once"
        return self._login_settings_error()

    def _custom_kernel_unsupported_reason(self):
        if self.support.kind != QemuBuildKind.STAGE:
            return "Custom kernel packages can be used with stage archives."
        from .architecture import Architecture
        architecture = self.project_directory.get_architecture()
        if architecture != Architecture.HOST:
            return "Building a custom kernel requires the project architecture to match this computer."
        if not self.snapshots:
            return "Add or download a Gentoo snapshot before building a custom kernel."
        toolset = self.project_directory.get_toolset()
        if not self.kernel_build_toolsets and toolset and toolset.busy_reason:
            return toolset.busy_reason
        if not self.kernel_build_toolsets:
            return "Select or create a free Gentoo toolset to build a custom kernel."
        return None

    def _selected_kernel_toolset(self):
        selected = self.kernel_toolset_row.get_selected()
        return self.kernel_build_toolsets[selected] if 0 <= selected < len(self.kernel_build_toolsets) else None

    def _valid_custom_kernel(self):
        from .custom_kernel import valid_kernel_package
        valid = valid_kernel_package(self.kernel_package_row.custom_row.get_text())
        self.kernel_package_error.set_title(
            "Enter a package atom such as sys-kernel/gentoo-kernel, optionally qualified with ::repository.")
        self.kernel_package_error.set_visible(not valid)
        return valid

    def _load_kernel_packages(self):
        snapshot = self._selected_snapshot()
        self._kernel_package_load_generation += 1
        generation = self._kernel_package_load_generation
        self._kernel_packages_loading = True
        self.kernel_package_row.set_sensitive(False)
        self.wizard_view._refresh_buttons_state()
        threading.Thread(target=self._read_kernel_packages, args=(snapshot, generation), daemon=True).start()

    def _read_kernel_packages(self, snapshot, generation):
        try:
            from .project_kernel_packages import load_kernel_packages
            stage = next((item for item in self.project_directory.stages if item.id == self.build.stage_id), None)
            packages = load_kernel_packages(self.project_directory, stage, snapshot=snapshot) if stage is not None else []
        except Exception as error:
            print(f"Failed to load VM kernel packages: {error}")
            packages = []
        GLib.idle_add(lambda: self._kernel_packages_loaded(packages, generation) and False)

    def _kernel_packages_loaded(self, packages, generation):
        if generation != self._kernel_package_load_generation:
            return False
        self._kernel_packages_loading = False
        self.kernel_packages = [package for package in packages if package.supported]
        self.kernel_package_row.set_packages(self.kernel_packages)
        default = next((package.atom for package in self.kernel_packages
                        if package.atom == "sys-kernel/gentoo-kernel"),
                       self.kernel_packages[0].atom if self.kernel_packages else "sys-kernel/gentoo-kernel")
        self.kernel_package_row.load(default, None)
        if not self.kernel_package_row.custom_row.get_text():
            self.kernel_package_row.custom_row.set_text("sys-kernel/gentoo-kernel")
        self.kernel_package_row.set_sensitive(True)
        self._custom_kernel_changed()
        return True

    def _kernel_snapshot_changed(self, *_):
        if self.custom_kernel_row.get_active() and self.snapshots:
            self._load_kernel_packages()

    def _selected_snapshot(self):
        selected = self.kernel_snapshot_row.get_selected()
        return self.snapshots[selected] if 0 <= selected < len(self.snapshots) else None

    @staticmethod
    def _snapshot_label(snapshot):
        return f"{snapshot.name} · {snapshot.filename}"

    def _selected_kernel_package(self):
        return self.kernel_package_row.selected_atom()

    def _is_custom_kernel_selected(self):
        return self.kernel_package_row.is_custom_selected()

    def _custom_kernel_changed(self, *_):
        active = self.custom_kernel_row.get_active()
        custom = active and self._is_custom_kernel_selected()
        self.kernel_toolset_row.set_visible(active)
        self.kernel_snapshot_row.set_visible(active)
        self.kernel_package_row.set_visible(active)
        self.kernel_package_row.custom_row.set_visible(custom)
        self.kernel_cache_row.set_visible(active)
        if active:
            self._kernel_cache_generation += 1
            generation = self._kernel_cache_generation
            if self._kernel_cache_timeout:
                GLib.source_remove(self._kernel_cache_timeout)
            self._kernel_cache_timeout = GLib.timeout_add(350, self._check_kernel_cache, generation)
        if custom:
            self._valid_custom_kernel()
        else:
            self.kernel_package_error.set_visible(False)
        if hasattr(self, "wizard_view"):
            self.wizard_view._refresh_buttons_state()

    def _check_kernel_cache(self, generation):
        self._kernel_cache_timeout = 0
        atom = self._selected_kernel_package()
        if not atom:
            self.kernel_cache_row.set_subtitle("Select a kernel package to check its cache.")
            return GLib.SOURCE_REMOVE
        self.kernel_cache_row.set_subtitle("Checking for a prepared kernel…")
        threading.Thread(target=self._read_kernel_cache, args=(atom, generation), daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _read_kernel_cache(self, atom, generation):
        try:
            from .qemu_run import _kernel_output_cache_directory, _kernel_outputs_ready
            architecture = self.project_directory.get_architecture()
            toolset = self._selected_kernel_toolset()
            if architecture is not None and toolset is not None:
                output_cache = _kernel_output_cache_directory(
                    architecture.value, atom, toolset)
                if _kernel_outputs_ready(output_cache, atom):
                    manifest = os.path.join(output_cache, ".kernel-output")
                    with open(manifest, encoding="utf-8") as file:
                        _package, release = file.read().splitlines()
                    metadata_path = os.path.join(output_cache, ".kernel-build.json")
                    try:
                        with open(metadata_path, encoding="utf-8") as file:
                            metadata = json.load(file)
                        built_at = metadata.get("built_at", "unknown date")[:10]
                        snapshot_name = metadata.get("gentoo_snapshot", "unknown snapshot")
                        toolset_name = metadata.get("toolset", {}).get("name", "unknown toolset")
                        subtitle = (f"Cached {release} · {toolset_name} · {snapshot_name} · built {built_at}; reused by this run.")
                    except (OSError, ValueError, TypeError, AttributeError):
                        subtitle = f"Cached kernel output for release {release}; this run will reuse it."
                    GLib.idle_add(lambda: self._show_kernel_cache_status(subtitle, generation))
                    return
            subtitle = "No cached kernel output; this run will prepare the selected kernel package."
        except Exception as error:
            subtitle = f"Cache status unavailable: {error}"
        GLib.idle_add(lambda: self._show_kernel_cache_status(subtitle, generation))

    def _show_kernel_cache_status(self, subtitle, generation):
        if generation == self._kernel_cache_generation and self.custom_kernel_row.get_active():
            self.kernel_cache_row.set_subtitle(subtitle)
        return GLib.SOURCE_REMOVE

    def _backend_changed(self, *_):
        native = bool(self.backends) and self.backends[self.backend_row.get_selected()][1] == "apple"
        self.backend_row.set_subtitle("Uses Apple Virtualization with an embedded display. Requires a matching CPU architecture."
                                      if native else "Boots images using QEMU, including other CPU architectures.")
        limits = self.apple.limits if native else None
        cpu = self.cpus_row.get_adjustment()
        memory = self.memory_row.get_adjustment()
        cpu.set_lower(limits['min_cpus'] if limits else 1)
        cpu.set_upper(limits['max_cpus'] if limits else 64)
        memory.set_lower(max(1, (limits['min_memory'] + (1 << 30) - 1) // (1 << 30)) if limits else 1)
        memory.set_upper(limits['max_memory'] // (1 << 30) if limits else 256)

    def _on_realize(self, _widget):
        self.wizard_view._window = self._window
        self.wizard_view.content_navigation_view = getattr(self._window, "navigation_view", None)

    def _show_runtime(self, runtime, title):
        runtime._window = self._window
        runtime._page = getattr(self, "_page", None)
        runtime._header = getattr(self, "_header", None)
        if runtime._page is not None:
            runtime._page.set_title(title)
        elif isinstance(self._window, Adw.Window):
            self._window.set_title(title)
            self._window.set_default_size(1024, 768)
        if runtime._header is not None:
            for widget in runtime.header_bar_end_widgets():
                runtime._header.pack_end(widget)
        self.remove(self.wizard_view)
        self.append(runtime)
        runtime.start()

    def _start(self, _wizard):
        if getattr(self, "_launch_started", False):
            return
        if any(run.is_running for run in QemuRun.running_runs):
            dialog = Adw.AlertDialog(heading="A virtual machine is already running",
                                     body="Stop the running machine before starting another one.")
            dialog.add_response("ok", "OK")
            dialog.set_default_response("ok")
            dialog.present(self.get_root())
            return

        if not self.backends:
            return
        if error := self._account_settings_error():
            dialog = Adw.AlertDialog(heading="Invalid login settings", body=error)
            dialog.add_response("ok", "OK")
            dialog.present(self.get_root())
            return
        if self.kernel_required and (not self.custom_kernel_row.get_active() or self._custom_kernel_reason):
            return
        if self.custom_kernel_row.get_active():
            toolset = self._selected_kernel_toolset()
            if toolset is None or toolset.busy_reason:
                dialog = Adw.AlertDialog(heading="Toolset unavailable",
                                         body=toolset.busy_reason if toolset else "Select a free Gentoo toolset.")
                dialog.add_response("ok", "OK")
                dialog.present(self.get_root())
                return
        backend = self.backends[self.backend_row.get_selected()][1]
        # Do not launch a backend that disappeared after the wizard was opened.
        available = apple_support(self.project_directory, self.build) if backend == "apple" else qemu_support(self.project_directory, self.build)
        if not available.available:
            dialog = Adw.AlertDialog(heading="Backend unavailable", body=available.reason)
            dialog.add_response("ok", "OK")
            dialog.present(self.get_root())
            return
        self._launch_started = True
        cpus = round(self.cpus_row.get_value())
        memory_gib = round(self.memory_row.get_value())
        kernel_package = self._selected_kernel_package() if self.custom_kernel_row.get_active() else None
        local_users = [row.user() for row in self.user_rows] if self.support.kind == QemuBuildKind.STAGE else []
        root_password = self.root_password_entry.get_text() or None
        support = available.support if backend == "apple" else available
        support = replace(support, kernel_package=kernel_package,
                          kernel_snapshot=self._selected_snapshot() if kernel_package else None,
                          kernel_toolset=self._selected_kernel_toolset() if kernel_package else None,
                          local_users=local_users, root_password=root_password,
                          host_resolv_conf=(self.host_resolv_conf if self.host_dns_row.get_active() else None))
        title = f"{self.build.stage_name} — {'Apple Virtualization' if backend == 'apple' else 'QEMU'}"
        if support.kind == QemuBuildKind.STAGE:
            support = replace(support, image_directory=create_stage_image_directory())
            runtime = QemuRunView(self.project_directory, self.build, memory_mib=memory_gib * 1024,
                                  cpus=cpus, support=support, backend=backend)
            preparation = QemuImagePreparation(self.project_directory, self.build, support)
            self.wizard_view.install_view.on_finish_requested = lambda: self._finish_preparation(
                preparation, runtime, title)

            def prepared(success):
                if not success:
                    return
                preparation.mark_result_seen()

                def show_vm():
                    self._show_runtime(runtime, title)
                    return False
                if self.auto_close_on_finish:
                    GLib.idle_add(show_vm)

            preparation.on_completed = prepared
            preparation.start()
            self.wizard_view.set_installation(preparation)
        else:
            runtime = QemuRunView(self.project_directory, self.build, memory_mib=memory_gib * 1024,
                                  cpus=cpus, support=support, backend=backend)
            GLib.idle_add(lambda: (self._show_runtime(runtime, title), False)[1])

    def _finish_preparation(self, preparation, runtime, title):
        if preparation.status != MultiStageProcessState.COMPLETED:
            return False
        # Defer replacing the wizard while the Finish button handler is still
        # unwinding; closing/removing its current child synchronously can drop
        # the navigation page before the VM view is attached.
        GLib.idle_add(lambda: (self._show_runtime(runtime, title), GLib.SOURCE_REMOVE)[1])
        return True
