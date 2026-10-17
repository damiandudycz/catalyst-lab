from __future__ import annotations
import hashlib, os, re, shutil, tempfile, threading
from pathlib import Path
from dataclasses import replace
from gi.repository import Gtk, Gdk, Adw, GLib, Pango
from .qemu_run import (QemuRun, QemuRunEvent, QemuBuildKind, qemu_support, stage_image_ready,
                       stage_image_directory, stage_image_cache_directory, stage_base_image_ready,
                       stage_image_script, copy_stage_base_image, stage_image_mount_script,
                       stage_image_unmount_script, stage_image_add_modules_script,
                       create_stage_image_directory, cleanup_stage_image_directory)
from .custom_kernel import copy_kernel_outputs, is_arm64_linux_image
from .apple_virtualization import AppleVirtualizationRun, apple_support
from .vnc_view import VncView
from .cl_toggle_group import CLToggle, CLToggleGroup
from .usage_graph import UsageGraph, USAGE_INTERVAL_SECONDS
from .multistage_process import MultiStageProcess, MultiStageProcessStage, MultiStageProcessStageState, MultiStageProcessEvent, MultiStageProcessState
from .app_events import AppEvents, app_event_bus

# ------------------------------------------------------------------------------
# Build running in QEMU: screen of machine (VNC), its serial console and QEMU log, with power controls. A pristine stage
# image is cached per build; each run gets a writable copy in Library/Temporary that is cleaned up when it stops.

_ANSI = re.compile(r"\x1b\[[0-9;?=<>:]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\x1b[=>78]|\r")


def _visible_serial_text(text: str) -> str:
    text = _ANSI.sub("", text)
    return "".join(char for char in text if char in "\n\t" or char.isprintable())

def _gib(size: int) -> str:
    return f"{size / 1024 ** 3:.1f} GiB"


def _kernel_output_cache_directory(build, architecture: str, package: str, snapshot, overlays) -> str:
    from .repository import Repository
    # Portage configuration and the resulting stage can differ between builds even when
    # they select the same kernel atom. Keep compiled outputs isolated by build identity.
    signature = [str(build.id), os.path.realpath(build.artifact_path or build.path),
                 architecture, package, snapshot.filename]
    for path in (build.artifact_path or build.path, snapshot.file_path()):
        try:
            stat = os.stat(path)
            signature.append(f"{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            signature.append("missing")
    for name, path in overlays:
        signature.append(f"overlay:{name}:{os.path.realpath(path)}")
        category = os.path.join(path, "sys-kernel")
        try:
            for root, directories, files in os.walk(category):
                for filename in sorted(files):
                    if filename.endswith(".ebuild"):
                        ebuild = os.path.join(root, filename)
                        stat = os.stat(ebuild)
                        signature.append(f"{ebuild}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            continue
    key = hashlib.sha256("\n".join(signature).encode()).hexdigest()[:24]
    return os.path.join(Repository.Settings.value.cache_location, "TestVMKernels", key)


def _kernel_outputs_ready(directory: str, require_arm64_image: bool = False) -> bool:
    modules = os.path.join(directory, "custom-modules")
    try:
        kernel = os.path.join(directory, "custom-kernel")
        return (os.path.isfile(kernel)
                and (not require_arm64_image or is_arm64_linux_image(kernel))
                and os.path.isfile(os.path.join(directory, "custom-initramfs"))
                and os.path.isdir(modules) and any(os.scandir(modules)))
    except OSError:
        return False


def present_qemu_run(run: QemuRun):
    """Show an existing VM view, or create a fresh view attached to the running machine."""
    view = getattr(run, "view", None)
    if view is None:
        view = QemuRunView(run.project_directory, run.build, run=run)
        app_event_bus.emit(AppEvents.PRESENT_VIEW, view, f"{run.name} — {run.backend_name}", 1024, 768)
        return
    window = getattr(view, "_window", None)
    navigation = getattr(window, "navigation_view", None)
    page = getattr(view, "_page", None)
    if navigation is not None and page is not None:
        stack = navigation.get_navigation_stack()
        if any(stack.get_item(index) is page for index in range(stack.get_n_items())):
            navigation.pop_to_page(page)
            return
        # A view can remain referenced briefly after its page is removed.
        view._detach_run()
        present_qemu_run(run)
    elif window is not None:
        window.present()

class QemuImagePreparation(MultiStageProcess):
    """Creates disk image of stage for QEMU (in virtual machine on macOS, as root on Linux)."""

    def __init__(self, project_directory, build, support):
        self.project_directory = project_directory
        self.project_id = project_directory.id if project_directory else None
        self.build = build
        self.support = support
        self.failure_reason: str | None = None
        self.canceled = False
        self.on_completed = None
        self._completion_notified = False
        self._canceling = False
        super().__init__(title="Start virtual machine")

    def setup_stages(self):
        self.stages.append(_StepImage(multistage_process=self))

    def name(self) -> str:
        return self.build.stage_name

    def complete_process(self, success: bool):
        if self._completion_notified:
            return
        self._completion_notified = True
        success = success and not self.canceled
        if not success:
            cleanup_stage_image_directory(self.support.image_directory)
        if self.on_completed:
            self.on_completed(success)

    def cancel(self):
        self.cancel_async()

    def cancel_async(self):
        if self._canceling or self.status not in (MultiStageProcessState.SETUP, MultiStageProcessState.IN_PROGRESS,
                                                   MultiStageProcessState.COMPLETED):
            return
        self.canceled = True
        self._canceling = True
        active = next((stage for stage in self.stages
                       if stage.state == MultiStageProcessStageState.IN_PROGRESS), None)
        if active is not None:
            active.request_cancel()

        def stop_work():
            if active is not None:
                active.cancel_work()
            GLib.idle_add(self._finish_cancel)
        threading.Thread(target=stop_work, daemon=True).start()

    def _finish_cancel(self):
        if self.status != MultiStageProcessState.FAILED:
            self.status = MultiStageProcessState.FAILED
            self.event_bus.emit(MultiStageProcessEvent.STATE_CHANGED, self.status)
        for stage in self.stages:
            if stage.state in (MultiStageProcessStageState.IN_PROGRESS, MultiStageProcessStageState.SCHEDULED):
                stage._update_state(MultiStageProcessStageState.FAILED)
        self._cleanup()
        self.clean_from_started_processes()
        self.complete_process(success=False)
        return False

class _StepImage(MultiStageProcessStage):
    def __init__(self, multistage_process):
        super().__init__(name="Prepare VM image and kernel", description="Creates the writable VM image and builds the selected kernel",
                         multistage_process=multistage_process)

    def log(self, line: str):
        if line.startswith("@@extract-progress "):
            try:
                percent = max(0, min(100, int(line.split()[-1])))
            except ValueError:
                return
            self._update_progress(percent / 100)
            line = f"Extracting stage · {percent}%"
        super().log(line)

    def start(self):
        if self.multistage_process.canceled:
            self._cancel_event.set()
            self._update_state(MultiStageProcessStageState.FAILED)
            return
        self.processes = []
        self._kernel_call = None
        self._worker_thread = threading.current_thread()
        super().start()
        process = self.multistage_process
        connection = None
        try:
            from .deploy_image import image_connection
            build = process.build
            if not process.support.boot_kernel:
                raise RuntimeError("Stage has no boot kernel; select a custom kernel package to run it")
            directory = stage_image_directory(build, process.support.backend, process.support.kernel_package,
                                              process.support.kernel_snapshot, process.support.image_directory)
            os.makedirs(directory, exist_ok=True)
            connection = image_connection(process.project_directory)
            if error := connection.connect(process_holder=self.processes, output_handler=self.log,
                                           cancel_event=self._cancel_event):
                raise RuntimeError(error)
            self._check_canceled()
            # Size: stage unpacked (about 4 times its archive) and free space, sparse file takes only used space.
            size = max(8 << 30, (os.path.getsize(build.artifact_path) * 5 // (1 << 30) + 4) << 30)
            cache_directory = stage_image_cache_directory(build, process.support, size)
            if not stage_base_image_ready(cache_directory, process.support.kernel, process.support.initramfs):
                # Incomplete cache entries are discarded so a failed extraction cannot be reused.
                shutil.rmtree(cache_directory, ignore_errors=True)
                os.makedirs(cache_directory, exist_ok=True)
                self.log("Extracting stage into reusable cache image")
                script = stage_image_script(build.artifact_path, cache_directory, size, process.support.kernel,
                                            process.support.initramfs)
                if not connection.run(script, self.log, self.processes):
                    shutil.rmtree(cache_directory, ignore_errors=True)
                    raise RuntimeError("Failed to create cached base disk image")
                Path(cache_directory, ".ready").write_text("ready\n", encoding="utf-8")
            self._check_canceled()
            if process.support.backend == "qemu" and not process.support.kernel_package:
                # QEMU's writable qcow2 overlay can use the cached raw disk directly as its backing.
                # This avoids copying a multi-GiB image even when Cache and Temporary are on
                # different filesystems or the library volume does not support reflinks.
                process.support.base_image_path = os.path.join(cache_directory, "root.img")
                process.support.base_kernel_path = os.path.join(cache_directory, process.support.kernel)
                process.support.base_initramfs_path = (os.path.join(cache_directory, process.support.initramfs)
                                                       if process.support.initramfs else None)
                self.log("Using cached base image directly with a disposable QEMU overlay")
            else:
                cloned = copy_stage_base_image(cache_directory, directory, process.support.kernel,
                                               process.support.initramfs, self.processes, self._cancel_event)
                self.log("Created copy-on-write clone of cached base image" if cloned else
                         "Copied cached base image; this filesystem does not support instant clones")
            self._check_canceled()
            if process.support.kernel_package:
                if not connection.run(stage_image_mount_script(directory), self.log, self.processes):
                    raise RuntimeError("Failed to mount the temporary VM image for kernel preparation")
                self._check_canceled()
                try:
                    self._emerge_kernel_package(directory)
                    self._check_canceled()
                    if not connection.run(stage_image_add_modules_script(directory), self.log, self.processes):
                        raise RuntimeError("Failed to add custom kernel modules to the temporary VM image")
                finally:
                    if not connection.run(stage_image_unmount_script(directory), self.log, self.processes):
                        raise RuntimeError("Failed to unmount the temporary VM image")
            self._check_canceled()
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            if self._cancel_event.is_set():
                return
            self.multistage_process.failure_reason = str(e)
            self.log(f"Failed to prepare VM disk image: {e}")
            print(f"Failed to create QEMU image: {e}")
            self.complete(MultiStageProcessStageState.FAILED)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _check_canceled(self):
        if self._cancel_event.is_set():
            raise RuntimeError("VM startup canceled")

    def cancel(self):
        if self.state != MultiStageProcessStageState.IN_PROGRESS:
            return
        self.request_cancel()
        self.cancel_work()

    def request_cancel(self):
        self._cancel_event.set()

    def cancel_work(self):
        call = getattr(self, "_kernel_call", None)
        if call is not None:
            if not getattr(self, "_kernel_cancel_sent", False):
                self._kernel_cancel_sent = True
                call.cancel()
        for running in getattr(self, "processes", []):
            try:
                if running.poll() is None:
                    running.terminate()
            except (AttributeError, OSError):
                continue
        worker = getattr(self, "_worker_thread", None)
        if worker and worker is not threading.current_thread():
            worker.join()

    def _emerge_kernel_package(self, directory: str):
        """Build a kernel in a disposable project-toolset overlay and export its boot files."""
        import shlex
        from .custom_kernel import kernel_package_build_command
        from .repository import Repository
        from .root_helper_server import ServerResponseStatusCode
        from .toolset import BindMount
        from .project_build_spec import generate_portage_confdir
        from .rootless import prepare_snapshot_squashfs, unmount_squashfs, distfiles_directory
        from .project_kernel_packages import _stage_overlays
        from .project_stage_arguments import StageArgumentDetails
        from .project_stage_value_resolver import resolve_stage_argument

        process = self.multistage_process
        project = process.project_directory
        snapshot = process.support.kernel_snapshot
        if snapshot is None:
            raise RuntimeError("Select a Gentoo snapshot to emerge a custom kernel")
        stage = next((item for item in project.stages if item.id == process.build.stage_id), None)
        overlays = _stage_overlays(project, stage) if stage is not None else []
        package_cache = None
        if stage is not None:
            package_cache = resolve_stage_argument(project, stage, StageArgumentDetails.pkgcache_path.value)
            if isinstance(package_cache, str) and package_cache.strip():
                package_cache = os.path.realpath(os.path.expanduser(package_cache.strip()))
            else:
                package_cache = None
        architecture = project.get_architecture()
        cache_directory = _kernel_output_cache_directory(
            process.build, architecture.value if architecture else "unknown",
            process.support.kernel_package, snapshot, overlays)
        require_arm64_image = (process.support.backend == "apple"
                               and process.support.machine.binary == "qemu-system-aarch64")
        if _kernel_outputs_ready(cache_directory, require_arm64_image):
            copy_kernel_outputs(cache_directory, directory)
            return

        toolset = project.get_toolset()
        if toolset is None:
            raise RuntimeError("A project toolset is required to build a custom kernel")
        if not toolset.reserve():
            raise RuntimeError(toolset.busy_reason or f"Toolset {toolset.name} is in use")

        # Mount the machine workspace before preparing Portage bindings. spawn acquires a second
        # workspace reference and releases it on unspawn.
        workspace_machine = toolset.machine
        workspace_user = f"Preparing VM kernel for {process.build.stage_name}"
        workspace_acquired = False
        temporary = os.path.realpath(os.path.expanduser(Repository.Settings.value.temporary_location))
        os.makedirs(temporary, exist_ok=True)
        config_directory = tempfile.mkdtemp(prefix="test-vm-kernel-", dir=temporary)
        spawned = False
        snapshot_root = None
        snapshot_mounted = False
        os.makedirs(cache_directory, exist_ok=True)
        try:
            if workspace_machine is not None:
                workspace_machine.acquire_workspace(output_handler=self.log, user=workspace_user)
                workspace_acquired = True
            executor = toolset.executor
            snapshot_root, snapshot_mounted = prepare_snapshot_squashfs(
                snapshot.file_path(), self.log, executor=executor)

            if stage is not None:
                generate_portage_confdir(project, stage, config_directory)
            repos_directory = Path(config_directory, "repos.conf")
            repos_directory.mkdir(parents=True, exist_ok=True)

            bindings = [
                BindMount(mount_path="/var/db/repos/gentoo", host_path=snapshot_root),
                BindMount(mount_path="/mnt/catalystlab-kernel-output", host_path=cache_directory,
                          store_changes=True, create_if_missing=True),
                BindMount(mount_path="/mnt/catalystlab-stage-portage", host_path=config_directory),
                BindMount(mount_path="/var/cache/distfiles", host_path=distfiles_directory(executor),
                          store_changes=True, create_if_missing=True),
            ]
            use_binary_packages = False
            if package_cache:
                try:
                    info = executor.filesystem({"info": [package_cache]})["info"].get(package_cache, {})
                    if info.get("type") == "dir":
                        bindings.append(BindMount(mount_path="/var/cache/binpkgs", host_path=package_cache,
                                                  read_only=True))
                        use_binary_packages = True
                        self.log(f"Using build binary package cache read-only: {package_cache}")
                except Exception as error:
                    self.log(f"Build binary package cache is unavailable; packages will be built from source: {error}")
            repo_lines = ["[gentoo]", "location = /var/db/repos/gentoo", "auto-sync = no", ""]
            for overlay_name, overlay_path in overlays:
                overlay = next((item for item in Repository.OverlayDirectory.value
                                if item.directory_path() == overlay_path), None)
                repo_name = overlay.repo_name() if overlay else os.path.basename(overlay_path.rstrip(os.sep))
                if not repo_name or any(character.isspace() or character in "[]" for character in repo_name):
                    continue
                repo_lines.extend([f"[{repo_name}]", f"location = /var/db/repos/{repo_name}",
                                   "masters = gentoo", "auto-sync = no", ""])
                bindings.append(BindMount(mount_path=f"/var/db/repos/{repo_name}",
                                          host_path=overlay_path))
            repo_config = repos_directory / "99-catalystlab-test-vm.conf"
            repo_config.write_text("\n".join(repo_lines), encoding="utf-8")

            toolset.spawn(store_changes=False, additional_bindings=bindings)
            spawned = True
            pkgdir_config = ('printf \'%s\\n\' \'PKGDIR="/var/cache/binpkgs"\' >> /etc/portage/make.conf; '
                             if use_binary_packages else "")
            command = ("set -e; mkdir -p /etc/portage; "
                       "cp -a /mnt/catalystlab-stage-portage/. /etc/portage/; "
                       + pkgdir_config
                       + kernel_package_build_command(process.support.kernel_package, use_binary_packages,
                                                      require_arm64_image))
            completed = threading.Event()
            response = []
            self._kernel_cancel_sent = False
            call = toolset.run_command(command=command, handler=self.log,
                                       completion_handler=lambda result: (response.append(result), completed.set()))
            self._kernel_call = call
            while call.thread and call.thread.is_alive():
                call.thread.join(timeout=0.1)
                if self._cancel_event.is_set() and not self._kernel_cancel_sent:
                    self._kernel_cancel_sent = True
                    call.cancel()
            self._kernel_call = None
            completed.wait()
            if not response or response[-1].code != ServerResponseStatusCode.OK:
                output = call.get_output() if hasattr(call, "get_output") else getattr(call, "output", [])
                details = "\n".join(output[-30:])
                suffix = f":\n{details}" if details else ""
                raise RuntimeError(f"Portage failed to emerge the custom kernel{suffix}")
        finally:
            try:
                if spawned:
                    toolset.unspawn(rebuild_squashfs_if_needed=False)
            finally:
                try:
                    if snapshot_mounted and snapshot_root:
                        unmount_squashfs(snapshot_root, self.log, executor=toolset.executor)
                finally:
                    try:
                        if workspace_acquired:
                            workspace_machine.release_workspace(output_handler=self.log, user=workspace_user)
                    finally:
                        toolset.release()
                        shutil.rmtree(config_directory, ignore_errors=True)
                        if not _kernel_outputs_ready(cache_directory, require_arm64_image):
                            shutil.rmtree(cache_directory, ignore_errors=True)
        if not _kernel_outputs_ready(cache_directory, require_arm64_image):
            raise RuntimeError("Kernel package did not produce reusable boot files")
        copy_kernel_outputs(cache_directory, directory)

class QemuRunView(Gtk.Box):
    """Shows a QEMU machine; closing the view detaches it without stopping the machine."""

    def __init__(self, project_directory, build, content_navigation_view: Adw.NavigationView | None = None,
                 memory_mib: int = 4096, cpus: int = 4, start_immediately: bool = False,
                 run: QemuRun | None = None, backend: str = "qemu", support=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.project_directory = project_directory
        self.build = build
        self.backend = "apple" if isinstance(run, AppleVirtualizationRun) else backend
        self.backend_name = "Apple Virtualization" if self.backend == "apple" else "QEMU"
        self.apple = apple_support(project_directory, build) if self.backend == "apple" else None
        self.support = run.support if run is not None else (support or (
            self.apple.support if self.apple and self.apple.support else
            qemu_support(project_directory, build, check_runtime=self.backend == "qemu")))
        if self.apple is not None and support is not None:
            self.apple = replace(self.apple, support=self.support)
        self.run: QemuRun | None = None
        self._serial_pending = ""
        self._auto_console_detection = False
        self._native_input_active = False
        self._native_display_suspended = False
        self._native_pressed_keys = {}
        self._native_input_root = None
        self._native_event_controller = None
        self.preparation: QemuImagePreparation | None = None
        self._launch_cpus = cpus
        self._launch_memory_mib = memory_mib
        self._start_immediately = start_immediately
        # The view selector is added to the window's top header panel, like stage view options.
        self.view_toggle = CLToggleGroup(valign=Gtk.Align.CENTER)
        self.view_toggle.add_css_class("round")
        self.view_toggle.add_css_class("caption")
        for title in ("Screen", "Console", "Log"):
            self.view_toggle.add(CLToggle(label=title))
        self.view_toggle.set_active(0)
        self.view_toggle.connect("notify::active", self._on_view_toggle)
        self.stack = Adw.ViewStack(vexpand=True)
        self.stack.connect("notify::visible-child-name", self._on_stack_page_changed)

        bar = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        self.status_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.status_label.add_css_class("caption")
        bar.append(self.status_label)
        self.cancel_start_button = Gtk.Button(label="Cancel startup", icon_name="process-stop-symbolic",
                                              tooltip_text="Cancel virtual machine startup", visible=False)
        self.cancel_start_button.add_css_class("flat")
        self.cancel_start_button.connect("clicked", self._cancel_vm_startup)
        bar.append(self.cancel_start_button)
        self.buttons = []
        for icon, tooltip, handler in (("system-shutdown-symbolic", "Power off (ACPI, system shuts down)", lambda: self.run.power_off()),
                                       ("view-refresh-symbolic", "Reset", lambda: self.run.reset()),
                                       ("input-keyboard-symbolic", "Send Ctrl+Alt+Del", lambda: self.run.send_ctrl_alt_delete()),
                                       ("media-playback-stop-symbolic", "Cancel startup or stop machine immediately", lambda: self.run.stop())):
            button = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER, sensitive=False)
            button.add_css_class("flat")
            button.connect("clicked", lambda _, handler=handler: self.run and GLib.idle_add(lambda: (handler(), False)[1]))
            bar.append(button)
            self.buttons.append(button)
        if self.backend == "apple":
            # Apple supplies a native NSView for input/display rather than a VNC server.
            for button in self.buttons[1:3]:
                button.set_visible(False)
            self.keyboard_capture_button = Gtk.ToggleButton(icon_name="input-keyboard-symbolic",
                tooltip_text="Capture keyboard for the VM (Ctrl+Alt+G releases it)", sensitive=False)
            self.keyboard_capture_button.add_css_class("flat")
            self.keyboard_capture_button.connect("toggled", lambda button: self._capture_native_input(button.get_active()))
            bar.append(self.keyboard_capture_button)
            self.buttons.append(self.keyboard_capture_button)
        self.control_bar = bar
        bar.set_visible(False)
        self.append(bar)
        self.progress_bar = Gtk.ProgressBar(width_request=240, halign=Gtk.Align.CENTER, visible=False)
        self._progress_timer: int | None = None
        # Status and display are separate pages so messages never overlap controls.
        self.vnc_view = VncView()
        self.vnc_view.status_handler = self._set_screen_status
        screen = Gtk.Stack(vexpand=True, hexpand=True)
        screen.add_named(self.vnc_view, "display")
        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                             halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                             margin_start=32, margin_end=32, margin_top=32, margin_bottom=32)
        self.status_spinner = Gtk.Spinner(width_request=32, height_request=32,
                                          halign=Gtk.Align.CENTER, visible=False)
        status_box.append(self.status_spinner)
        self.status_heading = Gtk.Label(label="Virtual machine", wrap=True, justify=Gtk.Justification.CENTER)
        self.status_heading.add_css_class("title-2")
        status_box.append(self.status_heading)
        self.screen_status = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=52)
        self.screen_status.add_css_class("dim-label")
        status_box.append(self.screen_status)
        status_box.append(self.progress_bar)
        screen.add_named(status_box, "status")
        if self.backend == "apple":
            self.native_display = Gtk.DrawingArea(hexpand=True, vexpand=True)
            self.native_display.connect("map", self._sync_native_display)
            self.native_display.connect("unmap", lambda _: self._detach_native_display())
            self.native_display.add_tick_callback(self._sync_native_display)
            screen.add_named(self.native_display, "native")
        screen.set_visible_child_name("status")
        self._screen_widget = screen
        self._screen_css = Gtk.CssProvider()
        self._screen_css.load_from_data(
            b".qemu-screen { background-color: #000; } "
            b".console-clear, .console-clear text { background-color: transparent; }"
        )
        Gtk.StyleContext.add_provider_for_display(self.get_display(), self._screen_css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.stack.add_titled_with_icon(screen, "screen", "Screen", "video-display-symbolic")
        self.console_view, console = self._text_view(editable_input=True)
        console_placeholder_text = (
            "This ISO may not enable a serial console. Use Screen to view and interact with the guest."
            if self.support.kind == QemuBuildKind.ISO else
            "No serial output received yet. The guest may not have started a serial console."
        )
        self.console_placeholder = Gtk.Label(label=console_placeholder_text,
                                             wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=60,
                                             halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                             margin_start=24, margin_end=24, margin_top=24, margin_bottom=24)
        self.console_placeholder.add_css_class("dimmed")
        self.console_placeholder.set_can_target(False)
        console_page = Gtk.Overlay(child=console)
        console_page.add_css_class("console-clear")
        self.console_view.add_css_class("console-clear")
        self.console_scroller = console
        self.console_page = console_page
        console.connect("map", lambda _: self._schedule_console_layout_refresh())
        console_page.set_hexpand(True)
        console_page.set_vexpand(True)
        # An empty serial console has nothing to scroll. Enable vertical scrolling
        # only after the guest starts sending output.
        console.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        console_page.add_overlay(self.console_placeholder)
        self.stack.add_titled_with_icon(console_page, "console", "Console", "utilities-terminal-symbolic")
        self.log_view, log = self._text_view(editable_input=False)
        if self.backend == "apple":
            self.usage_cpu_row = Adw.ActionRow(title="CPUs allocated")
            self.usage_memory_row = Adw.ActionRow(title="Memory allocated")
            self.usage_cpu_row.set_subtitle(f"{run.cpus if run else cpus} vCPUs")
            self.usage_memory_row.set_subtitle(f"{(run.memory_mib if run else memory_mib) / 1024:g} GiB")
            description = "CPU and memory show configured limits. Live host CPU and memory usage is unavailable for Apple Virtualization."
        else:
            self.usage_cpu_row = self._usage_row("CPU (host)")
            self.usage_memory_row = self._usage_row("Memory (resident)")
            description = "Host-side usage."
        self.usage_disk_row = self._usage_row("Disk storage (host)")
        self.usage_disk_row.set_visible(self.support.kind == QemuBuildKind.STAGE)
        if self.support.kind == QemuBuildKind.STAGE:
            description += " Disk shows storage allocated to the VM's writable image, not free space inside the guest."
        self.usage_group = Adw.PreferencesGroup(
            title="VM resources" if self.backend == "apple" else "VM usage",
            description=description, visible=False,
        )
        self.usage_group.add(self.usage_cpu_row)
        self.usage_group.add(self.usage_memory_row)
        self.usage_group.add(self.usage_disk_row)
        qemu_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=24, margin_end=24,
                            margin_top=12, margin_bottom=24)
        qemu_page.append(self.usage_group)
        log_title = Gtk.Label(label=f"{self.backend_name} log", xalign=0, margin_top=6)
        log_title.add_css_class("heading")
        log_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        log_content.append(log_title)
        log_content.append(log)
        self.log_placeholder = Gtk.Label(label="No virtual machine log output to show yet.", wrap=True,
                                         halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.log_placeholder.add_css_class("dimmed")
        self.log_stack = Gtk.Stack(vexpand=True, hexpand=True)
        self.log_stack.add_named(self.log_placeholder, "empty")
        self.log_stack.add_named(log_content, "output")
        qemu_page.append(self.log_stack)
        self._update_log_page()
        self.stack.add_titled_with_icon(qemu_page, "log", "Log", "text-x-generic-symbolic")
        self._usage_timer: int | None = None
        self._reading_usage = False
        self.connect("map", lambda _: self._start_usage_updates())
        self.connect("unmap", lambda _: self._stop_usage_updates())
        self.append(self.stack)
        # Closing the page detaches this UI; the QEMU process remains alive and can be reopened from the side menu.
        self.connect("realize", self._on_realize)
        self.connect("unrealize", lambda _: self._detach_run())
        self._set_status("Starting virtual machine…" if start_immediately else "Virtual machine stopped")
        self._set_screen_status("Starting virtual machine…" if start_immediately else "Virtual machine stopped")
        if run is not None:
            self._attach_run(run)
        elif start_immediately:
            GLib.idle_add(self.start)

    def header_bar_end_widgets(self) -> list[Gtk.Widget]:
        return [self.view_toggle]

    def _on_view_toggle(self, toggle_group, _param):
        names = ("screen", "console", "log")
        index = toggle_group.get_active()
        if 0 <= index < len(names):
            self.stack.set_visible_child_name(names[index])

    def _on_stack_page_changed(self, stack, _param):
        names = ("screen", "console", "log")
        name = stack.get_visible_child_name()
        if name != "screen":
            self._detach_native_display()
        if name in names and self.view_toggle.get_active() != names.index(name):
            self.view_toggle.set_active(names.index(name))
        if name == "console" and hasattr(self, "console_page"):
            # ViewStack can initially allocate a newly shown page before the
            # window settles. Repeat the resize after it has been on screen.
            self._schedule_console_layout_refresh()

    def _schedule_console_layout_refresh(self):
        GLib.timeout_add(100, self._refresh_console_layout)
        GLib.timeout_add(350, self._refresh_console_layout)
        return False

    def _refresh_console_layout(self):
        if self.stack.get_visible_child_name() != "console" or not self.console_scroller.get_mapped():
            return GLib.SOURCE_REMOVE
        vertical = self.console_scroller.get_vadjustment()
        follow_output = vertical.get_upper() - vertical.get_value() <= vertical.get_page_size() + 2
        for widget in (self.console_page, self.console_scroller, self.console_view):
            widget.queue_resize()
        # A delayed allocation can leave GTK's adjustments at the old page size.
        # Keep the console at the left edge and preserve live-output tail-following.
        self.console_scroller.get_hadjustment().set_value(
            self.console_scroller.get_hadjustment().get_lower())
        if follow_output:
            vertical.set_value(max(vertical.get_lower(), vertical.get_upper() - vertical.get_page_size()))
        return GLib.SOURCE_REMOVE

    def _text_view(self, editable_input: bool):
        view = Gtk.TextView(monospace=True, editable=False, cursor_visible=editable_input,
                            wrap_mode=Gtk.WrapMode.CHAR, hexpand=True, vexpand=True,
                            top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        if editable_input:
            # Typed keys go to serial console of machine.
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self._on_console_key)
            view.add_controller(keys)
        scrolled = Gtk.ScrolledWindow(child=view, hexpand=True, vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        return view, scrolled

    def _append(self, view: Gtk.TextView, text: str):
        if view is self.console_view and not text.strip():
            return
        buffer = view.get_buffer()
        buffer.insert(buffer.get_end_iter(), text)
        if buffer.get_line_count() > 5000:
            buffer.delete(buffer.get_start_iter(), buffer.get_iter_at_line(buffer.get_line_count() - 5000)[1])
        view.scroll_to_iter(buffer.get_end_iter(), 0, False, 0, 1)
        if view is self.log_view:
            self._update_log_page()
        elif view is self.console_view:
            self.console_placeholder.set_visible(not buffer.get_text(
                buffer.get_start_iter(), buffer.get_end_iter(), True).strip())

    def _on_console_key(self, controller, keyval, keycode, state) -> bool:
        if self.run is None:
            return False
        special = {Gdk.KEY_Return: b"\r", Gdk.KEY_KP_Enter: b"\r", Gdk.KEY_BackSpace: b"\x7f", Gdk.KEY_Tab: b"\t",
                   Gdk.KEY_Escape: b"\x1b", Gdk.KEY_Up: b"\x1b[A", Gdk.KEY_Down: b"\x1b[B", Gdk.KEY_Right: b"\x1b[C",
                   Gdk.KEY_Left: b"\x1b[D"}
        if keyval in special:
            data = special[keyval]
        elif state & Gdk.ModifierType.CONTROL_MASK and Gdk.KEY_a <= Gdk.keyval_to_lower(keyval) <= Gdk.KEY_z:
            data = bytes([Gdk.keyval_to_lower(keyval) - Gdk.KEY_a + 1])
        elif (character := Gdk.keyval_to_unicode(keyval)) and character >= 32:
            data = chr(character).encode()
        else:
            return False
        self.run.send_serial(data)
        return True

    # Running

    def _set_status(self, text: str):
        self.status_label.set_label(text)

    def _show_progress(self, text: str):
        self._screen_widget.remove_css_class("qemu-screen")
        if text.startswith("Preparing disk image"):
            heading = "Preparing disk image"
        elif text.startswith("Building custom kernel"):
            heading = "Building custom kernel"
        else:
            heading = "Starting virtual machine"
        self.status_heading.set_label(heading)
        detail = text.split(" · ", 1)[-1] if " · " in text else text
        self.screen_status.set_label(detail)
        self.screen_status.set_visible(True)
        self._screen_widget.set_visible_child_name("status")
        self.control_bar.set_visible(False)
        self.status_spinner.set_visible(True)
        self.status_spinner.start()
        self.progress_bar.set_visible(True)
        if self._progress_timer is None:
            self._progress_timer = GLib.timeout_add(100, self._pulse_progress)

    def _pulse_progress(self) -> bool:
        if not self.progress_bar.get_visible():
            self._progress_timer = None
            return GLib.SOURCE_REMOVE
        self.progress_bar.pulse()
        return GLib.SOURCE_CONTINUE

    def _hide_progress(self):
        self.status_spinner.stop()
        self.status_spinner.set_visible(False)
        self.progress_bar.set_visible(False)
        self.progress_bar.set_show_text(False)
        if self._progress_timer is not None:
            GLib.source_remove(self._progress_timer)
            self._progress_timer = None

    def _set_screen_status(self, text: str, heading: str | None = None):
        self.screen_status.set_label(text)
        self.screen_status.set_visible(bool(text))
        if text:
            self._screen_widget.remove_css_class("qemu-screen")
            self.status_heading.set_label(heading or "Virtual machine")
            self._screen_widget.set_visible_child_name("status")
        else:
            self._screen_widget.set_visible_child_name("native" if self.backend == "apple" else "display")
        if "display output is not active" in text.lower():
            self._switch_to_console()
            self._auto_console_detection = False

    def _switch_to_console(self):
        if self.stack.get_visible_child_name() == "screen":
            self.stack.set_visible_child_name("console")

    def _set_running(self, running: bool):
        ready = running and (self.backend == "qemu" or self.run.ready)
        starting = running and not ready
        self.control_bar.set_visible(ready or starting)
        self.cancel_start_button.set_visible(starting)
        self.cancel_start_button.set_sensitive(starting)
        if ready:
            self._hide_progress()
            self._set_screen_status("")
        for button in self.buttons:
            button.set_sensitive(ready)
        if ready:
            self._screen_widget.add_css_class("qemu-screen")
        else:
            self._screen_widget.remove_css_class("qemu-screen")

    def _cancel_vm_startup(self, *_):
        run = self.run
        if run is None or not run.is_running:
            return
        self.cancel_start_button.set_sensitive(False)
        self.cancel_start_button.set_label("Canceling…")
        run.stop()

    def _attach_run(self, run: QemuRun):
        self.run = run
        run.view = self
        run.event_bus.subscribe(QemuRunEvent.STATE_CHANGED, self._on_state_changed, self)
        run.event_bus.subscribe(QemuRunEvent.SERIAL_OUTPUT, self._on_serial, self)
        run.event_bus.subscribe(QemuRunEvent.OUTPUT_LINE_ADDED, self._on_output_line, self)
        if run.output:
            self.log_view.get_buffer().set_text("\n".join(run.output) + "\n")
        if run.serial_output:
            text = _visible_serial_text(run.serial_output)
            if text.strip():
                self.console_view.get_buffer().set_text(text)
                self.console_placeholder.set_visible(False)
                self.console_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            else:
                self.console_view.get_buffer().set_text("")
                self.console_placeholder.set_visible(True)
                self.console_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        self._set_running(run.is_running)
        self.usage_group.set_visible(run.is_running)
        if run.is_running:
            self._set_status(self._running_status(run))
            if self.backend == "qemu":
                self.vnc_view.connect_vnc(run.vnc_socket, lambda: self.run is run and run.is_running)
        else:
            self._set_status("Stopped: " + run.error if run.error else "Stopped")
        self._update_log_page()

    def _detach_native_display(self):
        self._capture_native_input(False)
        if isinstance(self.run, AppleVirtualizationRun):
            self.run.detach_display()

    def suspend_native_display(self) -> bool:
        """Detach the AVF NSView while a GTK modal is shown above this window."""
        if self.backend != "apple":
            return False
        self._native_display_suspended = True
        self._detach_native_display()
        return True

    def resume_native_display(self):
        if self.backend == "apple":
            self._native_display_suspended = False
            GLib.idle_add(self._resume_native_display)

    def _resume_native_display(self):
        if hasattr(self, "native_display"):
            self._sync_native_display(self.native_display)
        return GLib.SOURCE_REMOVE

    def _capture_native_input(self, active):
        active = active and isinstance(self.run, AppleVirtualizationRun) and self.run.ready
        if active == self._native_input_active:
            return
        self._native_input_active = active
        if isinstance(self.run, AppleVirtualizationRun):
            if not active:
                # Release held keys/modifiers when switching tabs, losing focus or navigating away.
                for code, text in self._native_pressed_keys.items():
                    self.run.send_key(code, False, 0, text)
                self._native_pressed_keys.clear()
            self.run.focus_display(active)
            self._set_status(self._running_status(self.run))
        self.keyboard_capture_button.set_active(active)

    def _native_pointer_press(self, gesture, count, x, y):
        valid, bounds = self.native_display.compute_bounds(self._native_input_root)
        inside = (valid and self.stack.get_visible_child_name() == "screen"
                  and bounds.get_x() <= x < bounds.get_x() + bounds.get_width()
                  and bounds.get_y() <= y < bounds.get_y() + bounds.get_height())
        button_valid, button_bounds = self.keyboard_capture_button.compute_bounds(self._native_input_root)
        over_button = (button_valid and button_bounds.get_x() <= x < button_bounds.get_x() + button_bounds.get_width()
                       and button_bounds.get_y() <= y < button_bounds.get_y() + button_bounds.get_height())
        if not over_button:
            self._capture_native_input(inside)
        gesture.set_state(Gtk.EventSequenceState.DENIED)

    def _native_key(self, keyval, code, state, pressed):
        if not self._native_input_active or not isinstance(self.run, AppleVirtualizationRun) or not self.run.ready:
            return False
        if (pressed and Gdk.keyval_to_lower(keyval) == Gdk.KEY_g
                and state & Gdk.ModifierType.CONTROL_MASK and state & Gdk.ModifierType.ALT_MASK):
            self._capture_native_input(False)
            return True
        flags = 0
        for modifier, bit in ((Gdk.ModifierType.LOCK_MASK, 16), (Gdk.ModifierType.SHIFT_MASK, 17),
                              (Gdk.ModifierType.CONTROL_MASK, 18), (Gdk.ModifierType.ALT_MASK, 19),
                              (Gdk.ModifierType.META_MASK | Gdk.ModifierType.SUPER_MASK, 20)):
            if state & modifier:
                flags |= 1 << bit
        character = Gdk.keyval_to_unicode(keyval)
        text = chr(character) if character else ""
        if pressed:
            self._native_pressed_keys[code] = text
        else:
            self._native_pressed_keys.pop(code, None)
        self.run.send_key(code, pressed, flags, text)
        return True # Consume before GTK navigation and application shortcuts.

    def _sync_native_display(self, widget, frame_clock=None):
        if not isinstance(self.run, AppleVirtualizationRun) or not self.run.ready:
            return True
        if self._native_display_suspended:
            self.run.detach_display()
            return True
        native = widget.get_native()
        if (not widget.get_mapped() or native is None or self.stack.get_visible_child_name() != "screen"
                or self._screen_widget.get_visible_child_name() != "native"):
            self.run.detach_display()
            return True
        import gi
        gi.require_version("GdkMacos", "4.0")
        from gi.repository import GdkMacos
        surface = native.get_surface()
        valid, bounds = widget.compute_bounds(native)
        if not valid or not isinstance(surface, GdkMacos.MacosSurface):
            self.run.detach_display()
            return True
        self.run.place_display(surface.get_native_window(), bounds.get_x(), bounds.get_y(),
                               bounds.get_width(), bounds.get_height())
        return True

    def _running_status(self, run):
        if self.backend == "apple":
            if not run.ready:
                return "Starting Apple Virtualization…"
            return ("Keyboard captured · Ctrl+Alt+G to release" if self._native_input_active else
                    "Running with Apple Virtualization · Click the screen to type")
        speed = f"accelerated ({run.accelerator.upper()})" if run.accelerator else "emulated (slow)"
        return f"Running, {speed} · SSH: ssh -p {run.ssh_port} root@localhost"

    def _detach_run(self):
        self._detach_native_display()
        if self._native_event_controller is not None:
            self._native_input_root.remove_controller(self._native_event_controller)
            self._native_input_root.remove_controller(self._native_pointer_controller)
            self._native_input_root.disconnect(self._native_focus_handler)
            self._native_event_controller = None
            self._native_input_root = None
        self._stop_usage_updates()
        self._auto_console_detection = False
        if self.run is not None:
            run = self.run
            for event in (QemuRunEvent.STATE_CHANGED, QemuRunEvent.SERIAL_OUTPUT, QemuRunEvent.OUTPUT_LINE_ADDED):
                run.event_bus.unsubscribe(event, self)
            if getattr(run, "view", None) is self:
                run.view = None
            self.run = None
        self.vnc_view.disconnect_vnc()
    def start(self, *_):
        if any(run is not self.run and run.is_running for run in QemuRun.running_runs):
            self._set_status("Only one test virtual machine can run at a time")
            self._set_screen_status("Another test virtual machine is already running")
            return
        availability = self.apple if self.backend == "apple" else self.support
        if not availability.available:
            self._set_status(availability.reason or "Backend is unavailable")
            self._set_screen_status(availability.reason or "Backend is unavailable")
            return
        if self.support.kind == QemuBuildKind.STAGE and not self.support.image_directory:
            self.support = replace(self.support, image_directory=create_stage_image_directory())
            if self.apple is not None:
                self.apple = replace(self.apple, support=self.support)
        self._auto_console_detection = True
        if self.support.kind == QemuBuildKind.STAGE and not stage_image_ready(self.build, self.support):
            self._prepare_image()
            return
        self._show_progress("Starting virtual machine…")
        if self.backend == "apple":
            run = AppleVirtualizationRun(self.project_directory, self.build, self.apple,
                                        memory_mib=self._launch_memory_mib, cpus=self._launch_cpus)
        else:
            run = QemuRun(self.project_directory, self.build, self.support,
                          memory_mib=self._launch_memory_mib, cpus=self._launch_cpus)
        self._attach_run(run)
        self.log_view.get_buffer().set_text("")
        self._update_log_page()
        self.console_view.get_buffer().set_text("")
        self.console_placeholder.set_visible(True)
        self.console_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        try:
            run.start()
        except Exception as e:
            self._hide_progress()
            self._auto_console_detection = False
            self._detach_run()
            self.run = None
            cleanup_stage_image_directory(self.support.image_directory)
            self._set_status(f"Failed to start {self.backend_name}: {e}")
            self._set_screen_status(f"Failed to start {self.backend_name}: {e}")
            return
        if self.backend == "qemu":
            self.vnc_view.connect_vnc(run.vnc_socket, lambda: self.run is run and run.is_running)
        if self.backend == "qemu":
            self._hide_progress()
            self._set_screen_status("Starting machine…")

    def _prepare_image(self):
        self._set_status("Preparing disk image…")
        self._show_progress("Preparing disk image · Creating a writable VM image.")
        self.preparation = QemuImagePreparation(self.project_directory, self.build, self.support)
        from .multistage_process import MultiStageProcessStageEvent
        # Kept: event bus refers to handlers weakly.
        self._preparation_log = lambda line: GLib.idle_add(lambda: (self._on_preparation_output(line), False)[1])
        for stage in self.preparation.stages:
            stage.event_bus.subscribe(MultiStageProcessStageEvent.OUTPUT_LINE_ADDED, self._preparation_log)
        self.preparation.event_bus.subscribe(MultiStageProcessEvent.STATE_CHANGED, self._on_preparation_state, self)
        self.preparation.start()

    def _on_preparation_output(self, line: str):
        # GLib callbacks can arrive after extraction and VM startup have already finished.
        # Don't let queued progress overwrite the stopped or running VM state.
        if self.preparation is None or self.preparation.status != MultiStageProcessState.IN_PROGRESS:
            return
        if self.run is not None and self.run.is_running:
            return
        if line.startswith("@@extract-progress "):
            try:
                percent = max(0, min(100, int(line.split()[-1])))
            except ValueError:
                return
            self._show_progress(f"Preparing disk image · Extracting stage · {percent}% of archive read")
            if self._progress_timer is not None:
                GLib.source_remove(self._progress_timer)
                self._progress_timer = None
            self.progress_bar.set_fraction(percent / 100)
            self.progress_bar.set_show_text(False)
            return
        if line.startswith("Emerging custom kernel package "):
            self._set_status("Building custom kernel…")
            self._show_progress("Building custom kernel · Starting Portage emerge.")
            return
        if line.startswith(">>> Emerging "):
            self._set_status("Building custom kernel…")
            self._show_progress(f"Building custom kernel · {line}")
            return
        # Image preparation output is repetitive shell chatter, not VM output. Keep it out
        # of the VM log tab; extraction progress is shown in the status view above.

    def _on_preparation_state(self, state):
        def update():
            if state == MultiStageProcessState.COMPLETED:
                self._show_progress("Preparing disk image · starting virtual machine")
                self.preparation.mark_result_seen()
                self.start()
            elif state == MultiStageProcessState.FAILED:
                self._hide_progress()
                reason = self.preparation.failure_reason if self.preparation else None
                self._set_status(f"Failed to prepare VM image: {reason}" if reason else
                                 "Failed to prepare VM image; see Log tab for details")
                self._set_screen_status(reason or "See the Log tab for details.",
                                        heading="VM image preparation failed")
                if self.preparation and self.preparation.stages:
                    lines = self.preparation.stages[-1].output_lines
                    self.log_view.get_buffer().set_text("\n".join(lines[-200:]))
                    self._update_log_page()
            return False
        GLib.idle_add(update)

    def _on_state_changed(self, run: QemuRun):
        def update():
            if run is not self.run:
                return False
            running = run.is_running
            self._set_running(running)
            if running:
                if isinstance(run, AppleVirtualizationRun) and run.ready:
                    self._capture_native_input(run.input_captured)
                self._set_status(self._running_status(run))
            else:
                self._set_status(f"Stopped: {run.error}" if run.error else "Stopped")
                self._hide_progress()
                self._set_screen_status(f"The virtual machine stopped: {run.error}" if run.error else "The virtual machine has stopped.")
                self.vnc_view.disconnect_vnc()
                self.vnc_view.clear()
                self.console_view.get_buffer().set_text("")
                self.console_placeholder.set_visible(True)
                self.console_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
            self.log_view.get_buffer().set_text("\n".join(run.output) + "\n" if run.output else "")
            self._update_log_page()
            self.usage_group.set_visible(running)
            if not running:
                self._clear_usage()
            return False
        GLib.idle_add(update)

    def _on_output_line(self, line: str):
        if line.startswith("DISK_PROGRESS:"):
            try:
                percent = max(0, min(100, int(line.partition(":")[2])))
            except ValueError:
                return
            self._show_progress(f"Preparing disk image · Creating temporary VM disk from cached image · {percent}%")
            if self._progress_timer is not None:
                GLib.source_remove(self._progress_timer)
                self._progress_timer = None
            self.progress_bar.set_fraction(percent / 100)
            self.progress_bar.set_show_text(False)
            # Keep the selected Log tab useful during preparation without adding a
            # new line for every progress update.
            self.log_view.get_buffer().set_text(f"Preparing temporary disk image… {percent}%\n")
            self._update_log_page()
            return
        if line.startswith("No guest serial output after "):
            self._set_status("Apple Virtualization is running · No guest serial output")
            self.console_placeholder.set_label(line)
        self._append(self.log_view, line + "\n")

    def _update_log_page(self):
        if not hasattr(self, "log_stack"):
            return
        has_output = self.log_view.get_buffer().get_char_count() > 0
        self.log_stack.set_visible_child_name("output" if has_output else "empty")

    @staticmethod
    def _usage_row(title: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle="Waiting for sample…")
        row.graph = UsageGraph()
        row.add_suffix(row.graph)
        return row

    def _start_usage_updates(self):
        if self._usage_timer is None:
            self._usage_timer = GLib.timeout_add_seconds(USAGE_INTERVAL_SECONDS, self._read_usage)
            self._read_usage()

    def _stop_usage_updates(self):
        if self._usage_timer is not None:
            GLib.source_remove(self._usage_timer)
            self._usage_timer = None

    def _read_usage(self) -> bool:
        run = self.run
        if run is not None and run.is_running and not self._reading_usage:
            self._reading_usage = True
            def work():
                usage = run.read_usage()
                disk_usage = run.read_disk_usage()
                GLib.idle_add(self._show_usage, run, usage, disk_usage)
            threading.Thread(target=work, daemon=True).start()
        return True

    def _show_usage(self, run: QemuRun, usage: tuple[float, int] | None,
                    disk_usage: tuple[int, int] | None):
        self._reading_usage = False
        if run is not self.run or not run.is_running:
            return False
        self.usage_group.set_visible(True)
        if self.backend == "apple":
            self.usage_cpu_row.set_subtitle(f"{run.cpus} vCPUs")
            self.usage_memory_row.set_subtitle(f"{run.memory_mib / 1024:g} GiB")
        elif usage is None:
            self.usage_cpu_row.set_subtitle("Usage sample unavailable")
            self.usage_memory_row.set_subtitle("Usage sample unavailable")
        else:
            cpu_fraction, resident_bytes = usage
            self.usage_cpu_row.graph.add(cpu_fraction)
            self.usage_cpu_row.set_subtitle(f"{cpu_fraction:.0%} of {run.cpus} vCPUs")
            memory_limit = run.memory_mib * 1024 * 1024
            self.usage_memory_row.graph.add(min(resident_bytes / memory_limit, 1) if memory_limit else 0)
            self.usage_memory_row.set_subtitle(f"{_gib(resident_bytes)} of {run.memory_mib / 1024:.1f} GiB configured")
        if self.support.kind == QemuBuildKind.STAGE:
            if disk_usage is None:
                self.usage_disk_row.set_subtitle("Storage sample unavailable")
            else:
                allocated_bytes, virtual_bytes = disk_usage
                fraction = min(allocated_bytes / virtual_bytes, 1) if virtual_bytes else 0
                self.usage_disk_row.graph.add(fraction)
                self.usage_disk_row.set_subtitle(
                    f"{_gib(allocated_bytes)} used on host of {_gib(virtual_bytes)} virtual"
                )
        return False

    def _clear_usage(self):
        rows = [self.usage_disk_row]
        if self.backend == "qemu":
            rows += [self.usage_cpu_row, self.usage_memory_row]
        for row in rows:
            row.graph.clear()
            row.set_subtitle("Waiting for sample…")

    def _on_serial(self, text: str):
        def append():
            if (text and self.run is not None
                    and self.status_label.get_label().startswith("Apple Virtualization is running · No guest serial output")):
                self._set_status(self._running_status(self.run))
            # Control sequences can be split between reads, incomplete one waits for its rest.
            pending = self._serial_pending + text
            incomplete = re.search(r"\x1b[^A-Za-z]*$", pending)
            self._serial_pending = pending[incomplete.start():] if incomplete else ""
            complete = pending[:incomplete.start()] if incomplete else pending
            visible_text = _visible_serial_text(complete)
            if visible_text.strip():
                self.console_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
                self._append(self.console_view, visible_text)
            serial_output = self.run.serial_output.casefold() if self.run is not None else ""
            if self._auto_console_detection and any(message in serial_output for message in (
                    "display output is not active", "can't be loaded on", "could not load image")):
                self._switch_to_console()
                self._auto_console_detection = False
            return False
        GLib.idle_add(append)

    def _on_realize(self, widget):
        if self.backend == "apple" and self._native_event_controller is None:
            self._native_input_root = self.get_native()
            self._native_event_controller = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
            self._native_event_controller.connect("key-pressed", lambda _, key, code, state: self._native_key(key, code, state, True))
            self._native_event_controller.connect("key-released", lambda _, key, code, state: self._native_key(key, code, state, False))
            self._native_input_root.add_controller(self._native_event_controller)
            self._native_pointer_controller = Gtk.GestureClick(button=0, propagation_phase=Gtk.PropagationPhase.CAPTURE)
            self._native_pointer_controller.connect("pressed", self._native_pointer_press)
            self._native_input_root.add_controller(self._native_pointer_controller)
            self._native_focus_handler = self._native_input_root.connect("notify::is-active", lambda window, _: (
                self._capture_native_input(False) if not window.is_active() else None))
        page = self.get_ancestor(Adw.NavigationPage)
        navigation = self.get_ancestor(Adw.NavigationView)
        if page is not None and navigation is not None and not getattr(self, "_page_observed", False):
            self._page_observed = True
            def hidden(_):
                stack = navigation.get_navigation_stack()
                if all(stack.get_item(index) is not page for index in range(stack.get_n_items())):
                    self._detach_run()
            page.connect("hidden", hidden)

    def stop(self):
        if self.run is not None and self.run.is_running:
            self.run.stop()
        self.vnc_view.disconnect_vnc()
