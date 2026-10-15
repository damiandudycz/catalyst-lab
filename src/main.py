import sys
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Gio, Adw, Gdk
from .main_window import CatalystlabWindow
from .modules_scanner import scan_all_submodules
from . import app_info
from .root_helper_client import RootHelperClient
from .toolset_manager import ToolsetManager
from .snapshot_manager import SnapshotManager
from .releng_manager import RelengManager
from .overlay_manager import OverlayManager
from .project_manager import ProjectManager

class CatalystlabApplication(Adw.Application):
    """The main application singleton class."""

    def __init__(self):
        super().__init__(application_id='com.damiandudycz.CatalystLab',
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.restart_requested = False
        self.create_action('quit', self.on_quit_action, ['<primary>q'])
        self.create_action('about', self.on_about_action)
        self.create_action('preferences', self.on_preferences_action)
        scan_all_submodules("catalystlab")
        self._library_initialized = False
        from .repository import Repository
        if Repository.Settings.value.initial_setup_done:
            self.initialize_library()
        # Initialize the in-memory queue of build operations.
        from .build_queue import BuildQueue
        BuildQueue.shared()

    def initialize_library(self, reload_repositories: bool = False):
        """Load Library data and perform startup cleanup after its location has been confirmed."""
        from .repository import Repository
        repositories = (
            Repository.Toolset, Repository.Snapshot, Repository.RelengDirectory, Repository.OverlayDirectory,
            Repository.ProjectDirectory, Repository.BuildMachine, Repository.PackagesDirectory,
        )
        if reload_repositories:
            for repository in repositories:
                repository.reload()
        startup_cleanup_needed = not self._library_initialized
        # Before any operation starts, they keep their data there.
        if startup_cleanup_needed:
            from .rootless import clear_temporary_directory
            clear_temporary_directory()
            # A killed app can leave the large sparse workspace image behind. It is safe to
            # remove at startup because operation registrations are not restored across runs.
            for machine in Repository.BuildMachine.value:
                try:
                    machine.cleanup_workspace()
                except Exception as e:
                    print(f"Failed to clean working space of virtual machine {machine.name}: {e}")
        ToolsetManager.shared().refresh()
        SnapshotManager.shared().refresh()
        RelengManager.shared().refresh()
        OverlayManager.shared().refresh()
        ProjectManager.shared().refresh()
        # Virtual machines (QEMU) left running by killed app.
        if startup_cleanup_needed:
            from .qemu_run import stop_orphaned_runs
            stop_orphaned_runs()
        # Unused snapshots are removed when enabled in Snapshots section.
        if startup_cleanup_needed:
            SnapshotManager.shared().start_auto_clean()
        self._library_initialized = True

    def do_activate(self):
        """Called when the application is activated.

        We raise the application's main window, creating it if
        necessary.
        """
        win = self.props.active_window
        if not win:
            win = CatalystlabWindow(application=self)
        win.present()

    def on_quit_action(self, *_):
        """Route Cmd+Q through the main window's close confirmation."""
        if self.props.active_window:
            self.props.active_window.close()
        else:
            self.quit()

    def do_shutdown(self):
        """Called when the application is shutting down."""
        if getattr(self, "_shutdown_sequence_completed", False):
            Gio.Application.do_shutdown(self)
            return
        RootHelperClient.shared().stop_root_helper()
        # Virtual machines started automatically are stopped with app.
        from .repository import Repository
        from .qemu_run import stop_all_runs
        stop_all_runs()
        for machine in Repository.BuildMachine.value:
            try:
                machine.stop_if_started_automatically()
            except Exception as e:
                print(f"Failed to stop virtual machine {machine.name}: {e}")
        Gio.Application.do_shutdown(self)

    def restart(self):
        """Quits and starts app again (eg. after library changed)."""
        self.restart_requested = True
        self.quit()

    def on_about_action(self, widget, _):
        """Callback for the app.about action."""
        about = Adw.AboutWindow(transient_for=self.props.active_window,
                                application_name='catalystlab',
                                application_icon='com.damiandudycz.CatalystLab',
                                developer_name='Unknown',
                                version=app_info.APP_VERSION,
                                developers=['Unknown'],
                                copyright='© 2025 Unknown')
        about.present()

    def on_preferences_action(self, widget, _):
        """Callback for the app.preferences action."""
        print('app.preferences action activated')

    def create_action(self, name, callback, shortcuts=None):
        """Add an application action.

        Args:
            name: the name of the action
            callback: the function to be called when the action is
              activated
            shortcuts: an optional list of accelerators
        """
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f"app.{name}", shortcuts)

def main(version):
    """The application's entry point."""
    app_info.set_app_version(version)
    app = CatalystlabApplication()
    result = app.run(sys.argv)
    if app.restart_requested:
        _start_again()
    return result

def _start_again():
    """Starts new instance of app, after this one finished (it's single instance app)."""
    import os, subprocess
    if getattr(sys, "frozen", False):
        bundle = os.path.dirname(os.path.dirname(os.path.dirname(sys.executable)))
        if sys.platform == "darwin" and bundle.endswith(".app"):
            subprocess.Popen(["open", "-n", bundle])
            return
        # Started executable of PyInstaller is new instance, not part of this one.
        os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
    os.execv(sys.executable, [sys.executable, *sys.argv])
