from __future__ import annotations
import os
import threading
from gi.repository import Gtk, Adw, GLib
from dataclasses import replace
from .wizard_view import WizardView
from .qemu_run import QemuRun, QemuBuildKind, qemu_support, create_stage_image_directory
from .qemu_run_view import QemuImagePreparation, QemuRunView
from .apple_virtualization import apple_support
from .repository import Repository
from .app_events import app_event_bus, AppEvents


class QemuLaunchWizard(Gtk.Box):
    """Select an available VM backend and resources before launching a build."""

    def __init__(self, project_directory, build):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.project_directory = project_directory
        self.build = build
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
        self.wizard_view.install_button_label = "Start virtual machine"
        self.wizard_view.connect("is-page-ready-to-continue", self._page_ready)
        self.wizard_view.connect("begin-installation", self._start)
        self.append(self.wizard_view)
        self.connect("realize", self._on_realize)

        settings = Repository.Settings.value
        page = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        page.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                          margin_start=24, margin_end=24, margin_top=24, margin_bottom=24)

        stage_group = Adw.PreferencesGroup(title="Selected stage", description="Review the build to boot in a virtual machine.")
        architecture = project_directory.get_architecture()
        architecture_name = architecture.name if architecture else "Unknown"
        kind = "Live CD ISO" if self.support.kind == QemuBuildKind.ISO else "Stage archive"
        artifact = os.path.basename(build.artifact_path or build.artifact or "Unknown")
        for title, value in (("Project", project_directory.name), ("Stage", build.stage_name),
                             ("Architecture", architecture_name), ("Build type", kind), ("Artifact", artifact)):
            stage_group.add(Adw.ActionRow(title=title, subtitle=value))
        content.append(stage_group)

        settings_group = Adw.PreferencesGroup(title="Run settings",
                                               description="Defaults come from app settings. Changes here apply only to this run.")
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
        advanced_row = Adw.ExpanderRow(title="Advanced", subtitle="Optional VM launch settings")
        self.custom_kernel_row = Adw.SwitchRow(
            title="Build a kernel package",
            subtitle="Build it in the project toolset using the Gentoo snapshot and stage overlays. The stage does not need Portage.")
        self.custom_kernel_row.set_visible(self.support.kind == QemuBuildKind.STAGE)
        advanced_row.add_row(self.custom_kernel_row)
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
        advanced_row.add_row(self.kernel_snapshot_row)
        self._kernel_package_load_generation = 0
        self._kernel_packages_loading = False
        self.kernel_packages = []
        self.kernel_package_row = Adw.ComboRow(
            title="Kernel package", model=Gtk.StringList.new(["Loading kernel packages…"]), enable_search=True)
        self.kernel_package_row.set_sensitive(False)
        advanced_row.add_row(self.kernel_package_row)
        self.custom_kernel_package_row = Adw.EntryRow(title="Custom package atom")
        self.custom_kernel_package_row.set_text("sys-kernel/gentoo-kernel")
        self.custom_kernel_package_row.set_visible(False)
        advanced_row.add_row(self.custom_kernel_package_row)
        self.kernel_package_error = Adw.ActionRow(title="Enter a package atom such as sys-kernel/gentoo-kernel, optionally qualified with ::repository.")
        self.kernel_package_error.add_css_class("warning")
        self.kernel_package_error.set_visible(False)
        advanced_row.add_row(self.kernel_package_error)
        settings_group.add(advanced_row)
        content.append(settings_group)

        unavailable = []
        if not self.support.available:
            unavailable.append(f"QEMU: {self.support.reason}")
        import sys
        if sys.platform == "darwin" and not self.apple.available:
            unavailable.append(f"Apple Virtualization: {self.apple.reason}")
        if unavailable:
            reason = Gtk.Label(label="\n".join(unavailable), xalign=0, wrap=True)
            reason.add_css_class("dimmed" if self.backends else "warning")
            content.append(reason)
        self.backend_row.connect("notify::selected", self._backend_changed)
        self.custom_kernel_row.connect("notify::active", self._custom_kernel_changed)
        self.kernel_snapshot_row.connect("notify::selected", self._kernel_snapshot_changed)
        self.kernel_package_row.connect("notify::selected", self._custom_kernel_changed)
        self.custom_kernel_package_row.connect("changed", self._custom_kernel_changed)
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
        page.set_child(content)
        self.wizard_view.carousel.append(page)
        self.wizard_view.pages.append(page)
        self.wizard_view._all_pages.append(page)
        self._custom_kernel_changed()
        self.wizard_view._refresh_buttons_state()
        if self.custom_kernel_row.get_visible() and self.snapshots:
            self._load_kernel_packages()

    def header_bar_end_widgets(self) -> list[Gtk.Widget]:
        return []

    def _page_ready(self, _wizard, _page) -> bool:
        return (bool(self.backends) and not any(run.is_running for run in QemuRun.running_runs)
                and (not self.kernel_required or self._custom_kernel_reason is None)
                and (not self.kernel_required or self.custom_kernel_row.get_active())
                and (not self.custom_kernel_row.get_active()
                     or (not self._kernel_packages_loading
                         and (not self._is_custom_kernel_selected() or self._valid_custom_kernel()))))

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
        if toolset is None:
            return "Select a project toolset to build a custom kernel."
        if toolset.busy_reason:
            return toolset.busy_reason
        return None

    def _valid_custom_kernel(self):
        from .custom_kernel import valid_kernel_package
        valid = valid_kernel_package(self.custom_kernel_package_row.get_text())
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
        labels = []
        for package in self.kernel_packages:
            overlays = [name for name in package.repositories if name != "gentoo"]
            labels.append(f"{package.atom} ({', '.join(overlays)})" if overlays else package.atom)
        labels.append("Custom package…")
        self.kernel_package_row.set_model(Gtk.StringList.new(labels))
        default_index = next((index for index, package in enumerate(self.kernel_packages)
                              if package.atom == "sys-kernel/gentoo-kernel"), 0)
        self.kernel_package_row.set_selected(default_index)
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
        selected = self.kernel_package_row.get_selected()
        if 0 <= selected < len(self.kernel_packages):
            return self.kernel_packages[selected].atom
        return self.custom_kernel_package_row.get_text().strip()

    def _is_custom_kernel_selected(self):
        selected = self.kernel_package_row.get_selected()
        return selected >= len(self.kernel_packages)

    def _custom_kernel_changed(self, *_):
        active = self.custom_kernel_row.get_active()
        custom = active and self._is_custom_kernel_selected()
        self.kernel_snapshot_row.set_visible(active)
        self.kernel_package_row.set_visible(active)
        self.custom_kernel_package_row.set_visible(custom)
        if custom:
            self._valid_custom_kernel()
        else:
            self.kernel_package_error.set_visible(False)
        if hasattr(self, "wizard_view"):
            self.wizard_view._refresh_buttons_state()

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

    def _start(self, _wizard):
        if any(run.is_running for run in QemuRun.running_runs):
            dialog = Adw.AlertDialog(heading="A virtual machine is already running",
                                     body="Stop the running machine before starting another one.")
            dialog.add_response("ok", "OK")
            dialog.set_default_response("ok")
            dialog.present(self.get_root())
            return

        if not self.backends:
            return
        if self.kernel_required and (not self.custom_kernel_row.get_active() or self._custom_kernel_reason):
            return
        backend = self.backends[self.backend_row.get_selected()][1]
        # Do not launch a backend that disappeared after the wizard was opened.
        available = apple_support(self.project_directory, self.build) if backend == "apple" else qemu_support(self.project_directory, self.build)
        if not available.available:
            dialog = Adw.AlertDialog(heading="Backend unavailable", body=available.reason)
            dialog.add_response("ok", "OK")
            dialog.present(self.get_root())
            return
        cpus = round(self.cpus_row.get_value())
        memory_gib = round(self.memory_row.get_value())
        kernel_package = self._selected_kernel_package() if self.custom_kernel_row.get_active() else None
        support = available.support if backend == "apple" else available
        support = replace(support, kernel_package=kernel_package,
                          kernel_snapshot=self._selected_snapshot() if kernel_package else None)
        title = f"{self.build.stage_name} — {'Apple Virtualization' if backend == 'apple' else 'QEMU'}"
        window_handle = getattr(self, "_window", None)
        if window_handle is not None:
            window_handle.close()
        if support.kind == QemuBuildKind.STAGE:
            support = replace(support, image_directory=create_stage_image_directory())
            runtime = QemuRunView(self.project_directory, self.build, memory_mib=memory_gib * 1024,
                                  cpus=cpus, support=support, start_immediately=True, backend=backend)
            preparation = QemuImagePreparation(self.project_directory, self.build, support)

            def prepared(success):
                if not success:
                    return
                preparation.mark_result_seen()

                def show_vm():
                    process_window = getattr(process_view, "_window", None)
                    if process_window is not None:
                        process_window.close()
                    app_event_bus.emit(AppEvents.PRESENT_VIEW, runtime, title, 1024, 768)
                    return False
                GLib.idle_add(show_vm)

            preparation.on_completed = prepared
            from .multistage_process_execution_view import MultistageProcessExecutionView
            process_view = MultistageProcessExecutionView()
            process_view.content_navigation_view = None
            preparation.start()
            process_view.set_multistage_process(preparation)
            app_event_bus.emit(AppEvents.PRESENT_VIEW, process_view, "Starting virtual machine", 640, 480)
        else:
            runtime = QemuRunView(self.project_directory, self.build, memory_mib=memory_gib * 1024,
                                  cpus=cpus, support=support, start_immediately=True, backend=backend)
            GLib.idle_add(lambda: (app_event_bus.emit(AppEvents.PRESENT_VIEW, runtime, title, 1024, 768), False)[1])
