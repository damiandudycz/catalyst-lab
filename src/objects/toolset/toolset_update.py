from __future__ import annotations
import os, threading, shutil, re, time
from .multistage_process import (
    MultiStageProcess, MultiStageProcessStage,
    MultiStageProcessStageState
)
from .toolset import Toolset
from .toolset_application import ToolsetApplication
from .root_function import root_function, local_for_rootless_paths
from .repository import Repository
from .root_helper_server import ServerResponse, ServerResponseStatusCode
from .helper_functions import  create_squashfs
from gi.repository import Gio
from .toolset_installation import portage_config_path, portage_patch_path, read_patch_content

# ------------------------------------------------------------------------------
# Toolset update.
# ------------------------------------------------------------------------------

class ToolsetUpdate(MultiStageProcess):
    """Handles the toolset update lifecycle. Also supports changing app selection, versions and patches."""
    def __init__(self, toolset: Toolset, allow_binpkgs: bool, update_packages: bool = True, apps_selection: list[ToolsetApplicationSelection] | None = None):
        self.toolset = toolset
        self.allow_binpkgs = allow_binpkgs
        self.update_packages = update_packages
        self.apps_selection = apps_selection
        self._process_selected_apps()
        super().__init__(title="Toolset update")

    def start(self, authorization_keeper: AuthorizationKeeper | None = None):
        if not self.toolset.reserve():
            raise RuntimeError("Failed to reserve toolset")
        super().start(authorization_keeper=authorization_keeper)

    def setup_stages(self):
        self.stages.append(ToolsetUpdateStepPrepareToolset(toolset=self.toolset, multistage_process=self))
        self.stages.append(ToolsetUpdateStepRefreshEnv(toolset=self.toolset, multistage_process=self))
        if self.update_packages:
            self.stages.append(ToolsetUpdateStepUpdatePortage(toolset=self.toolset, multistage_process=self))
        if self.apps_to_remove:
            for app_to_remove in self.apps_to_remove:
                self.stages.append(ToolsetUpdateStepUninstallApp(toolset=self.toolset, app=app_to_remove, multistage_process=self))
        if self.apps_selection:
            for app_selection in self.apps_selection:
                self.stages.append(ToolsetUpdateStepInstallApp(toolset=self.toolset, app_selection=app_selection, multistage_process=self))
                pass # Create task for modifying / adding entry
        if self.update_packages:
            self.stages.append(ToolsetUpdateStepUpdatePackages(toolset=self.toolset, multistage_process=self))
        self.stages.append(ToolsetUpdateStepVerify(toolset=self.toolset, multistage_process=self))
        self.stages.append(ToolsetUpdateStepStepCompress(toolset=self.toolset, multistage_process=self))
        super().setup_stages()

    def complete_process(self, success: bool):
        if success:
            # Update version_id of selected apps
            if self.apps_selection:
                for app_selection in self.apps_selection:
                    self.analysis_result.setdefault(app_selection.app.package, {})["version_id"] = str(app_selection.version.id)
            self.toolset.replace_metadata(self.analysis_result)
        self.toolset.release()

    def _process_selected_apps(self):
        """Manage auto_select dependencies."""
        if self.apps_selection is None:
            self.apps_to_remove = None
            return
        app_selections_by_app = { app_selection.app: app_selection for app_selection in self.apps_selection }
        # Mark all dependencies as selected
        for app_selection in self.apps_selection:
            if app_selection.selected:
                for dep in getattr(app_selection.app, "dependencies", []):
                    if dep in app_selections_by_app:
                        app_selections_by_app[dep] = app_selections_by_app[dep]._replace(selected=True)
        # Remove not selected entries
        self.apps_selection = [sel for sel in self.apps_selection if sel.selected]
        # Sort by dependencies
        sorted_entries: list[ToolsetApplicationSelection] = []
        def process_app_selection(app_selection: ToolsetApplicationSelection):
            for dep in getattr(app_selection.app, "dependencies", []):
                process_app_selection(app_selection=app_selections_by_app[dep])
            if not app_selection in sorted_entries:
                sorted_entries.append(app_selection)
        for app_selection in self.apps_selection:
            process_app_selection(app_selection=app_selection)
        self.apps_selection = sorted_entries
        apps_to_remove: list[ToolsetApplication] = []
        changed_apps: list[ToolsetApplicationSelection] = []
        for app in ToolsetApplication.ALL:
            new_app_install = next((app_selection for app_selection in self.apps_selection if app_selection.app == app), None) # ToolsetApplicationSelection
            previous_app_install = self.toolset.get_app_install(app=app) # ToolsetApplicationInstall
            # If app was never installed before now, no need to track changes
            if not previous_app_install:
                if new_app_install:
                    changed_apps.append(new_app_install)
                continue
            # If no new_app_install, mark app to remove
            if not new_app_install:
                apps_to_remove.append(app)
                continue
            removed_patches_filenames = [patch for patch in previous_app_install.patches if patch not in new_app_install.patches]
            added_patches_files = [patch for patch in new_app_install.patches if isinstance(patch, Gio.File)]
            variant_changed = new_app_install.version != previous_app_install.variant
            # Decide if there are any changes
            if removed_patches_filenames or added_patches_files or variant_changed:
                changed_apps.append(new_app_install)
        self.apps_selection = changed_apps
        self.apps_to_remove = apps_to_remove

# ------------------------------------------------------------------------------
# Update process steps.
# ------------------------------------------------------------------------------

class ToolsetUpdateStep(MultiStageProcessStage):
    """Base step for toolset update steps. Contains additional code for calling toolset commands."""
    """Stages that use run_command_in_toolset must have self.toolset available."""
    def start(self):
        self.server_call = None
        super().start()
    def cancel(self):
        super().cancel()
        if self.server_call:
            self.server_call.cancel()
            if self.server_call.thread:
                self.server_call.thread.join()
            self.server_call = None
    def run_command_in_toolset(self, command: str, progress_handler: Callable[[str], float | None] | None = None) -> bool:
        try:
            return_value = False
            done_event = threading.Event()
            def completion_handler(response: ServerResponse):
                nonlocal return_value
                return_value = response.code == ServerResponseStatusCode.OK
                done_event.set()
            def output_handler(output_line: str):
                print(output_line)
                progress = progress_handler(output_line)
                if progress is not None:
                    self._update_progress(progress)
            self.server_call = self.toolset.run_command(
                command=command,
                handler=output_handler if progress_handler is not None else None,
                completion_handler=completion_handler
            )
            self.server_call.thread.join()
            done_event.wait()
            self.server_call = None
            return return_value
        except Exception as e:
            print(f"Error running toolset command: {e}")
            self.complete(MultiStageProcessStageState.FAILED)
            return False

# Steps implementations:

class ToolsetUpdateStepPrepareToolset(ToolsetUpdateStep):
    def __init__(self, toolset: Toolset, multistage_process: MultiStageProcess):
        super().__init__(name="Prepare toolset", description="Spawns toolset with write access", multistage_process=multistage_process)
        self.toolset = toolset
    def start(self):
        super().start()
        try:
            if self.toolset.in_use:
                raise RuntimeError("Toolset is currently in use")
            def toolset_has_required_binding() -> bool:
                if not self.toolset.spawned:
                    return False
                if self.toolset.additional_bindings:
                    # There should be no additional bindings set
                    return False
                return True
            if self.toolset.spawned and (not toolset_has_required_binding() or not self.toolset.store_changes):
                # Toolset needs to be respawned to get correct bindings and write access
                self.toolset.unspawn()
            if not self.toolset.spawned:
                self.toolset.spawn(store_changes=True)
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during toolset preparation: {e}")
            self.complete(MultiStageProcessStageState.FAILED)
    def cleanup(self) -> bool:
        if not super().cleanup():
            return False
        self.toolset.unspawn(rebuild_squashfs_if_needed=False) # New squashFS is build as another step in update process.
        return True

class ToolsetUpdateStepRefreshEnv(ToolsetUpdateStep):
    def __init__(self, toolset: Toolset, multistage_process: MultiStageProcess):
        super().__init__(name="Refresh environment", description="Refreshes binpkg signing keys", multistage_process=multistage_process)
        self.toolset = toolset
    def start(self):
        super().start()
        try:
            # Prepare environment
            commands = [
                "env-update && source /etc/profile",
                "getuto"
            ]
            for i, command in enumerate(commands):
                if self._cancel_event.is_set():
                    return
                result = self.run_command_in_toolset(command=command)
                self._update_progress(i / len(commands))
                if not result:
                    raise RuntimeError(f"Command {command} failed")
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during keys refresh: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

class ToolsetUpdateStepUpdatePortage(ToolsetUpdateStep):
    def __init__(self, toolset: Toolset, multistage_process: MultiStageProcess):
        super().__init__(name="Synchronize portage", description="Synchronizes portage tree", multistage_process=multistage_process)
        self.toolset = toolset
    def start(self):
        super().start()
        try:
            def progress_handler(output_line: str) -> float or None:
                pattern = (
                    r"\s*"                        # optional leading spaces
                    r"\d+[KMGTP]?"                # downloaded size (e.g., 45500K, 4.47T)
                    r"\s+(?:\.{1,10}\s*)+"        # progress dots (at least one group)
                    r"(\d{1,3})%"                 # percentage (captured)
                    r"\s+\d+(\.\d+)?[KMGTP]?"     # speed (like 4.47T, 14.5M)
                    r"(?:[= ]\d+(\.\d+)?s?)?"     # optional time (e.g., =2.2s, 0s)"
                )
                match = re.match(pattern, output_line)
                if match:
                    return int(match.group(1)) / 100.0
            result = self.run_command_in_toolset(command="emerge-webrsync", progress_handler=progress_handler)
            self.complete(MultiStageProcessStageState.COMPLETED if result else MultiStageProcessStageState.FAILED)
        except Exception as e:
            print(f"Error synchronizing Portage: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

class ToolsetUpdateStepUninstallApp(ToolsetUpdateStep):
    def __init__(self, toolset: Toolset, app: ToolsetApplication, multistage_process: MultiStageProcess):
        super().__init__(name=f"Uninstall {app.name}", description=f"Removes {app.package} package", multistage_process=multistage_process)
        self.app = app
        self.toolset = toolset
    def start(self):
        super().start()
        try:
            def progress_handler(output_line: str) -> float or None:
                print(output_line)
            # Remove portage configs
            app_install = self.toolset.get_app_install(app=self.app)
            if not app_install:
                raise RuntimeError(f"App {self.app.name} was not found in toolset")
            self.toolset.remove_portage_files(
                [portage_config_path(config.directory, self.app.name) for config in (app_install.variant.config or [])]
                + [portage_patch_path(self.app.package, patch_file) for patch_file in app_install.patches]
            )
            flags = "-C"
            result = self.run_command_in_toolset(command=f"emerge {flags} {self.app.package}", progress_handler=progress_handler)
            self.complete(MultiStageProcessStageState.COMPLETED if result else MultiStageProcessStageState.FAILED)
        except Exception as e:
            print(f"Error during app uninstallation: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

class ToolsetUpdateStepInstallApp(ToolsetUpdateStep):
    def __init__(self, toolset: Toolset, app_selection: ToolsetApplicationSelection, multistage_process: MultiStageProcess):
        super().__init__(name=f"Install {app_selection.app.name}", description=f"Emerges {app_selection.app.package} package", multistage_process=multistage_process)
        self.toolset = toolset
        self.app_selection = app_selection
    def start(self):
        super().start()
        try:
            # Clean stuff from previous install
            app_install = self.toolset.get_app_install(app=self.app_selection.app)
            if app_install:
                removed_patches_filenames = [patch for patch in app_install.patches if patch not in self.app_selection.patches]
                self.toolset.remove_portage_files(
                    [portage_config_path(config.directory, self.app_selection.app.name) for config in (app_install.variant.config or [])]
                    + [portage_patch_path(self.app_selection.app.package, patch_file) for patch_file in removed_patches_filenames]
                )
            def progress_handler(output_line: str) -> float or None:
                pattern = r"^>>> Completed \((\d+) of (\d+)\)"
                match = re.match(pattern, output_line)
                if match:
                    n, m = map(int, match.groups())
                    return n / m
            files = {
                portage_config_path(config.directory, self.app_selection.app.name): "".join(entry + "\n" for entry in config.entries)
                for config in (self.app_selection.version.config or [])
            }
            files.update({
                portage_patch_path(self.app_selection.app.package, patch_file.get_basename()): read_patch_content(patch_file)
                for patch_file in self.app_selection.patches if isinstance(patch_file, Gio.File)
            })
            if files:
                self.toolset.write_portage_files(files)
            flags = "--getbinpkg --deep --update --changed-use --newuse" if self.multistage_process.allow_binpkgs else "--deep --update --changed-use --newuse"
            result = self.run_command_in_toolset(command=f"emerge {flags} {self.app_selection.app.package} --reinstall-atoms={self.app_selection.app.package}", progress_handler=progress_handler)
            self.complete(MultiStageProcessStageState.COMPLETED if result else MultiStageProcessStageState.FAILED)
        except Exception as e:
            print(f"Error during app installation: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

class ToolsetUpdateStepUpdatePackages(ToolsetUpdateStep):
    def __init__(self, toolset: Toolset, multistage_process: MultiStageProcess):
        super().__init__(name="Update packages", description="Updates installed packages", multistage_process=multistage_process)
        self.toolset = toolset
    def start(self):
        super().start()
        try:
            def progress_handler(output_line: str) -> float or None:
                pattern = r"^>>> Completed \((\d+) of (\d+)\)"
                match = re.match(pattern, output_line)
                if match:
                    n, m = map(int, match.groups())
                    return n / m
            allow_binpkgs = self.toolset.metadata.get('allow_binpkgs', False)
            flags = "--getbinpkg --changed-use --update --deep --with-bdeps=y" if allow_binpkgs else "--changed-use --update --deep --with-bdeps=y"
            result = self.run_command_in_toolset(command=f"emerge {flags} @system @world @live-rebuild", progress_handler=progress_handler)
            self.complete(MultiStageProcessStageState.COMPLETED if result else MultiStageProcessStageState.FAILED)
        except Exception as e:
            print(f"Error updating packages: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

class ToolsetUpdateStepVerify(ToolsetUpdateStep):
    def __init__(self, toolset: Toolset, multistage_process: MultiStageProcess):
        super().__init__(name="Analyze toolset", description="Collects information about toolset", multistage_process=multistage_process)
        self.toolset = toolset
    def start(self):
        super().start()
        try:
            analysis_result = self.toolset.analyze()
            analysis_result['date_updated'] = int(time.time())
            self.multistage_process.analysis_result = analysis_result
            self.toolset.write_metadata_to_json(metadata=analysis_result)
            self.complete(MultiStageProcessStageState.COMPLETED if analysis_result else MultiStageProcessStageState.FAILED)
        except Exception as e:
            print(f"Error during toolset verification: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

class ToolsetUpdateStepStepCompress(ToolsetUpdateStep):
    def __init__(self, toolset: Toolset, multistage_process: MultiStageProcess):
        super().__init__(name="Compress", description="Compresses toolset into .squashfs file", multistage_process=multistage_process)
        self.toolset = toolset
    def start(self):
        super().start()
        try:
            toolset_tmp_squashfs_path = self.toolset.file_path() + "_tmp"
            self.squashfs_process = self.toolset.create_squashfs(output_file=toolset_tmp_squashfs_path)
            for line in self.squashfs_process.stdout:
                line = line.strip()
                if line.isdigit():
                    percent = int(line)
                    self._update_progress(percent / 100.0)
            if self.squashfs_process.wait() != 0:
                raise RuntimeError("Failed to create squashfs file")
            self.squashfs_process = None
            shutil.move(toolset_tmp_squashfs_path, self.toolset.file_path())
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during toolset compression: {e}")
            self.complete(MultiStageProcessStageState.FAILED)
    def cleanup(self) -> bool:
        if not super().cleanup():
            return False
    def cancel(self):
        super().cancel()
        proc = self.squashfs_process
        if proc and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.squashfs_process = None

@root_function
def remove_portage_file(relative_path: str, toolset_root: str):
    """Removes file from /etc/portage of toolset."""
    path = os.path.join(toolset_root, "etc", "portage", relative_path)
    if os.path.isfile(path):
        os.remove(path)

remove_portage_file = local_for_rootless_paths(remove_portage_file, path_argument="toolset_root")
