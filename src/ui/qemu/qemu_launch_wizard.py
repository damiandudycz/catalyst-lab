from __future__ import annotations
import os
from gi.repository import Gtk, Adw, GLib
from .wizard_view import WizardView
from .qemu_run import QemuRun, QemuBuildKind, qemu_support
from .qemu_run_view import QemuRunView
from .repository import Repository
from .app_events import app_event_bus, AppEvents


class QemuLaunchWizard(Gtk.Box):
    """Select QEMU resources and review the stage before launching it."""

    def __init__(self, project_directory, build):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.project_directory = project_directory
        self.build = build
        self.support = qemu_support(project_directory, build)
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

        stage_group = Adw.PreferencesGroup(title="Selected stage", description="Review the build that will run in QEMU.")
        architecture = project_directory.get_architecture()
        architecture_name = architecture.name if architecture else "Unknown"
        kind = "Live CD ISO" if self.support.kind == QemuBuildKind.ISO else "Stage archive"
        artifact = os.path.basename(build.artifact_path or build.artifact or "Unknown")
        for title, value in (("Project", project_directory.name), ("Stage", build.stage_name),
                             ("Architecture", architecture_name), ("Build type", kind), ("Artifact", artifact)):
            stage_group.add(Adw.ActionRow(title=title, subtitle=value))
        content.append(stage_group)

        settings_group = Adw.PreferencesGroup(title="Run settings",
                                               description="These values start with your app settings and can be adjusted for this run.")
        self.cpus_row = Adw.SpinRow(title="CPUs", adjustment=Gtk.Adjustment(
            lower=1, upper=64, step_increment=1, value=settings.qemu_cpus))
        self.memory_row = Adw.SpinRow(title="Memory (GiB)", adjustment=Gtk.Adjustment(
            lower=1, upper=256, step_increment=1, value=settings.qemu_memory_gib))
        settings_group.add(self.cpus_row)
        settings_group.add(self.memory_row)
        content.append(settings_group)

        if not self.support.available:
            self.reason = Gtk.Label(label=self.support.reason or "This build cannot run in QEMU.",
                                    xalign=0, wrap=True)
            self.reason.add_css_class("warning")
            content.append(self.reason)
        page.set_child(content)
        self.wizard_view.carousel.append(page)
        self.wizard_view.pages.append(page)
        self.wizard_view._all_pages.append(page)
        self.wizard_view._refresh_buttons_state()

    def header_bar_end_widgets(self) -> list[Gtk.Widget]:
        return []

    def _page_ready(self, _wizard, _page) -> bool:
        return self.support.available and not any(run.is_running for run in QemuRun.running_runs)

    def _on_realize(self, _widget):
        self.wizard_view._window = self._window
        self.wizard_view.content_navigation_view = getattr(self._window, "navigation_view", None)

    def _start(self, _wizard):
        if any(run.is_running for run in QemuRun.running_runs):
            dialog = Adw.AlertDialog(heading="A QEMU machine is already running",
                                     body="Stop the running machine before starting another one.")
            dialog.add_response("ok", "OK")
            dialog.set_default_response("ok")
            dialog.present(self.get_root())
            return

        settings = Repository.Settings.value
        cpus = round(self.cpus_row.get_value())
        memory_gib = round(self.memory_row.get_value())
        settings.qemu_cpus = cpus
        settings.qemu_memory_gib = memory_gib
        Repository.Settings.save()
        runtime = QemuRunView(self.project_directory, self.build, memory_mib=memory_gib * 1024, cpus=cpus,
                              start_immediately=True)
        title = f"{self.build.stage_name} in QEMU"
        window_handle = getattr(self, "_window", None)
        if window_handle is not None:
            window_handle.close()
        GLib.idle_add(lambda: (app_event_bus.emit(AppEvents.PRESENT_VIEW, runtime, title, 1024, 768), False)[1])
