from __future__ import annotations

import threading
import time

from gi.repository import Adw, GLib, Gtk


def request_shutdown(window) -> bool:
    """Confirm and coordinate shutdown when operations are using app resources.

    Return True to keep the window open while the confirmation/progress UI is shown.
    """
    app = window.get_application()
    if getattr(app, "_shutdown_ready_to_close", False):
        return False
    if (getattr(app, "_shutdown_dialog", None) is not None
            or getattr(app, "_shutdown_confirmation", None) is not None):
        return True

    from .multistage_process import MultiStageProcess, MultiStageProcessState
    from .qemu_run import QemuRun
    from .build_queue import BuildQueue

    active = [process for process in MultiStageProcess.started_processes
              if process.status == MultiStageProcessState.IN_PROGRESS]
    qemu_runs = [run for run in QemuRun.running_runs if run.is_running]
    suspended_views = []
    for run in qemu_runs:
        view = getattr(run, "view", None)
        suspend = getattr(view, "suspend_native_display", None)
        if suspend is not None and suspend():
            suspended_views.append(view)
    queue = BuildQueue.shared()
    queued_count = len(queue.entries)
    if not active and not qemu_runs:
        return False

    body_items = [f"• {process.title}" for process in active]
    body_items.extend(f"• {run.name} in QEMU" for run in qemu_runs)
    if queued_count:
        body_items.append(f"• {queued_count} queued build{'s' if queued_count != 1 else ''}")
    body = ("These operations are still running:\n\n" + "\n".join(body_items)
            + "\n\nClosing will cancel the operations and stop app-managed machines.")
    dialog = Adw.AlertDialog(heading="Close Catalyst Lab?", body=body)
    dialog.add_response("keep", "Keep open")
    dialog.add_response("close", "Close and cancel")
    dialog.set_response_appearance("close", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("keep")
    dialog.set_close_response("keep")

    def on_response(_dialog, response):
        app._shutdown_confirmation = None
        if response == "close":
            # Wait for the confirmation surface to close before presenting another
            # modal surface over the same window.
            def show_shutdown_dialog():
                # The modal confirmation prevented new work before this snapshot.
                shutdown_dialog = ShutdownDialog(window, active, qemu_runs, queue)
                app._shutdown_dialog = shutdown_dialog
                shutdown_dialog.present(window)
                shutdown_dialog.start()
                return GLib.SOURCE_REMOVE

            GLib.idle_add(show_shutdown_dialog)
        else:
            # The user kept the app open; restore the embedded AVF display after
            # the confirmation surface has closed.
            GLib.idle_add(lambda: _resume_native_displays(suspended_views))

    dialog.connect("response", on_response)
    app._shutdown_confirmation = dialog
    dialog.present(window)
    return True


def _resume_native_displays(views):
    for view in views:
        view.resume_native_display()
    return GLib.SOURCE_REMOVE


class ShutdownDialog(Adw.Dialog):
    """Visible, sequential cleanup of running operations before app exit."""

    def __init__(self, window, processes, qemu_runs, build_queue):
        super().__init__(title="Closing Catalyst Lab", can_close=False, content_width=520,
                         presentation_mode=Adw.DialogPresentationMode.FLOATING)
        self.window = window
        self.app = window.get_application()
        self._steps = []
        self._rows = []
        self._errors = []
        self._current = 0

        if build_queue.entries:
            count = len(build_queue.entries)
            self._steps.append((f"Cancel {count} queued build{'s' if count != 1 else ''}", build_queue.clear))
        for process in processes:
            self._steps.append((f"Cancel {process.title}", lambda process=process: self._cancel_process(process)))
        for run in qemu_runs:
            self._steps.append((f"Stop {run.name} in QEMU", lambda run=run: self._stop_qemu(run)))

        from .repository import Repository
        for machine in Repository.BuildMachine.value:
            if machine.is_running and machine.stops_when_unused:
                self._steps.append((f"Stop virtual machine {machine.name}", machine.stop_if_started_automatically))

        from .root_helper_client import RootHelperClient
        helper = RootHelperClient.shared()
        if helper.is_server_process_running:
            self._steps.append(("Stop privileged helper", helper.stop_root_helper))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_start=24, margin_end=24, margin_top=24, margin_bottom=20)
        heading = Gtk.Label(label="Stopping operations", xalign=0)
        heading.add_css_class("heading")
        self.progress_bar = Gtk.ProgressBar(show_text=True)
        self.progress_bar.set_text("Preparing to close…")
        box.append(heading)
        box.append(self.progress_bar)

        self.rows_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.rows_box.add_css_class("boxed-list")
        for label, _action in self._steps:
            row = Adw.ActionRow(title=label, subtitle="Waiting")
            icon = Gtk.Image.new_from_icon_name("content-loading-symbolic")
            icon.set_visible(False)
            spinner = Gtk.Spinner()
            spinner.set_visible(False)
            row.add_prefix(icon)
            row.add_suffix(spinner)
            self.rows_box.append(row)
            self._rows.append((row, icon, spinner))
        if self._steps:
            self.rows_scroller = Gtk.ScrolledWindow(
                hscrollbar_policy=Gtk.PolicyType.NEVER,
                vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                propagate_natural_height=True,
                min_content_height=56,
            )
            self.rows_scroller.set_max_content_height(260)
            self.rows_scroller.set_child(self.rows_box)
            box.append(self.rows_scroller)

        self.status_label = Gtk.Label(label="The app will close after cleanup finishes.", xalign=0, wrap=True)
        self.status_label.add_css_class("dimmed")
        box.append(self.status_label)
        self.close_button = Gtk.Button(label="Force quit", halign=Gtk.Align.END)
        self.close_button.add_css_class("destructive-action")
        self.close_button.set_tooltip_text("Exit immediately without waiting for cleanup to finish")
        self.close_button.connect("clicked", lambda *_: self._finish_close(fallback=True))
        box.append(self.close_button)
        self.set_child(box)

        self._worker = None

    def start(self):
        if self._worker is None:
            self._worker = threading.Thread(target=self._run_steps, daemon=True, name="app-shutdown")
            self._worker.start()

    @staticmethod
    def _stop_qemu(run):
        run.stop()
        process = run.process
        if process is None:
            return
        try:
            process.wait(timeout=12)
        except Exception:
            # QemuRun.stop() already schedules terminate/kill escalation.
            deadline = time.monotonic() + 3
            while run.is_running and time.monotonic() < deadline:
                time.sleep(0.1)
            if run.is_running:
                raise RuntimeError("QEMU did not stop in time")

    @staticmethod
    def _cancel_process(process):
        from .multistage_process import MultiStageProcess, MultiStageProcessState
        if process in MultiStageProcess.started_processes and process.status == MultiStageProcessState.IN_PROGRESS:
            process.cancel()

    def _run_steps(self):
        total = len(self._steps)
        if not total:
            GLib.idle_add(self._finish_close)
            return
        for index, (label, action) in enumerate(self._steps):
            self._current = index
            self._run_on_ui(self._show_step_started, index, label, index, total)
            try:
                result = action()
                if result is False:
                    raise RuntimeError("Operation reported that it did not complete")
            except Exception as error:
                self._errors.append((label, str(error)))
                print(f"Shutdown step failed: {label}: {error}")
                self._run_on_ui(self._show_step_failed, index, str(error), index + 1, total)
            else:
                self._run_on_ui(self._show_step_finished, index, index + 1, total)
        self._run_on_ui(self._all_steps_finished)

    @staticmethod
    def _run_on_ui(callback, *args):
        """Run a GTK update on the main loop and wait for it before advancing cleanup."""
        completed = threading.Event()

        def invoke():
            try:
                callback(*args)
            finally:
                completed.set()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(invoke)
        completed.wait()

    def _show_step_started(self, index, label, completed, total):
        row, icon, spinner = self._rows[index]
        row.set_subtitle("In progress")
        spinner.set_visible(True)
        spinner.start()
        self.progress_bar.set_fraction(completed / total)
        self.progress_bar.set_text(f"{completed} of {total}")
        return GLib.SOURCE_REMOVE

    def _show_step_finished(self, index, completed, total):
        row, icon, spinner = self._rows[index]
        spinner.stop()
        spinner.set_visible(False)
        icon.set_from_icon_name("emblem-ok-symbolic")
        icon.set_visible(True)
        row.set_subtitle("Done")
        self.progress_bar.set_fraction(completed / total)
        self.progress_bar.set_text(f"{completed} of {total}")
        return GLib.SOURCE_REMOVE

    def _show_step_failed(self, index, message, completed, total):
        row, icon, spinner = self._rows[index]
        spinner.stop()
        spinner.set_visible(False)
        icon.set_from_icon_name("dialog-warning-symbolic")
        icon.set_visible(True)
        row.set_subtitle(message or "Could not stop")
        row.add_css_class("warning")
        self.progress_bar.set_fraction(completed / total)
        self.progress_bar.set_text(f"{completed} of {total}")
        return GLib.SOURCE_REMOVE

    def _all_steps_finished(self):
        if self._errors:
            self.status_label.set_label("Some items could not be stopped. Review the details or force quit.")
        else:
            self._finish_close()
        return GLib.SOURCE_REMOVE

    def _finish_close(self, fallback=False):
        self.app._shutdown_ready_to_close = True
        self.app._shutdown_sequence_completed = not fallback
        self.app._shutdown_dialog = None
        self.app.quit()
        return GLib.SOURCE_REMOVE
