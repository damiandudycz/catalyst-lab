from __future__ import annotations
import os, subprocess
from typing import final
from .multistage_process import (
    MultiStageProcess, MultiStageProcessStage,
    MultiStageProcessState, MultiStageProcessStageState
)
from .toolset import Toolset, BindMount
from .snapshot_manager import SnapshotManager
from .snapshot import Snapshot, existing_latest_snapshot
from .root_function import root_function
from .repository import Repository, resolve_path
from datetime import datetime
from .helper_functions import parse_strict_rfc_datetime
from .runner_operation import PrepareRunnerStage, StopRunnerStage
from .runner import default_runner

# ------------------------------------------------------------------------------
# Installation process.
# ------------------------------------------------------------------------------

@final
class SnapshotInstallation(MultiStageProcess):
    """Handles the full snapshot generation lifecycle."""
    def __init__(self, toolset: Toolset | None = None, runner=None, file: GLocalFile | None = None,
                 custom_filename: str | None = None):
        """Use either file or toolset, not both."""
        self.toolset = toolset
        self.runner = (runner or default_runner()) if toolset else None
        self.runner_prepared = False
        self.file = file
        self.custom_filename = custom_filename # Works only with file
        self.snapshot: Snapshot | None = None
        self.reused_snapshot = False # Snapshot of latest commit existed already, it was not generated again.
        super().__init__(title="Generating Portage snapshot")

    def setup_stages(self):
        if self.toolset:
            self.stages.append(PrepareRunnerStage(self))
            self.stages.append(SnapshotInstallationStepPrepareToolset(toolset=self.toolset, multistage_process=self))
            self.stages.append(SnapshotInstallationStepGenerateSnapshot(multistage_process=self))
        if self.file:
            self.stages.append(SnapshotInstallationStepCopyFile(file=self.file, custom_filename=self.custom_filename, multistage_process=self))
        self.stages.append(SnapshotInstallationStepSetupPermissions(multistage_process=self))
        self.stages.append(SnapshotInstallationStepAnalyze(multistage_process=self))
        if self.toolset:
            self.stages.append(StopRunnerStage(self))

    def should_prepare_runner(self, output_handler):
        if snapshot := existing_latest_snapshot(output_handler):
            self.snapshot = snapshot
            self.reused_snapshot = True
            return False
        return True

    def complete_process(self, success: bool):
        if success:
            SnapshotManager.shared().add_snapshot(self.snapshot)

    def name(self) -> str:
        return "Fetching snapshot"

# ------------------------------------------------------------------------------
# Installation process steps.
# ------------------------------------------------------------------------------

# Installation process steps.

@final
class SnapshotInstallationStepPrepareToolset(MultiStageProcessStage):
    def __init__(self, toolset: Toolset, multistage_process: MultiStageProcess):
        super().__init__(name="Prepare toolset", description="Spawns toolset with required bindings", multistage_process=multistage_process)
        self.toolset = toolset
        self.unspawn = False
        self.release = False
    def start(self):
        super().start()
        try:
            if self.multistage_process.reused_snapshot:
                self.complete(MultiStageProcessStageState.COMPLETED)
                return
            if self.toolset.in_use:
                raise RuntimeError("Toolset is currently in use")
            def toolset_has_required_binding() -> bool:
                if not self.toolset.spawned:
                    return False
                required_bindings = self.required_bindings()
                current_bindings = self.toolset.additional_bindings or []
                for required in required_bindings:
                    print(f"Checking if toolset contains: {required.mount_path}")
                    found = any(
                        existing.mount_path == required.mount_path and
                        existing.host_path == required.host_path and
                        existing.toolset_path == required.toolset_path
                        for existing in current_bindings
                    )
                    if not found:
                        print(f"Missing required binding: {required}")
                        return False
                return True
            if not self.toolset.reserve():
                raise RuntimeError("Failed to reserve toolset")
            self.release = True
            if self.toolset.spawned and not toolset_has_required_binding():
                # Toolset needs to be respawned to get correct bindings
                self.toolset.unspawn()
            if self.toolset.spawned and self.toolset.runner is not self.multistage_process.runner:
                self.toolset.unspawn()
            if not self.toolset.spawned:
                self.toolset.spawn(runner=self.multistage_process.runner,
                                   additional_bindings=self.required_bindings())
                self.unspawn = True
            else:
                self.unspawn = False
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during toolset preparation: {e}")
            self.complete(MultiStageProcessStageState.FAILED)
    def cleanup(self) -> bool:
        if not super().cleanup():
            return False
        if self.unspawn:
            self.toolset.unspawn()
        if self.release:
            self.toolset.release()
        return True
    def required_bindings(self) -> [BindMount]:
        return [
            BindMount(
                mount_path="/var/tmp/catalyst/snapshots",
                host_path=resolve_path(Repository.Settings.value.snapshots_location),
                store_changes=True,
                create_if_missing=True
            )
        ]

@final
class SnapshotInstallationStepCopyFile(MultiStageProcessStage):
    def __init__(self, file: GLocalFile, custom_filename: str | None, multistage_process: MultiStageProcess):
        super().__init__(name="Copy snapshot file", description="Copies snapshot file to repository location", multistage_process=multistage_process)
        self.file = file
        self.custom_filename = custom_filename
    def start(self):
        super().start()
        snapshot_copy_path = None
        copy_created = False
        try:
            def sanitize_filename_linux(name: str) -> str:
                return name.replace('/', '_').replace('\0', '_')
            filename = sanitize_filename_linux(self.custom_filename or self.file.get_basename())
            snapshots_location = resolve_path(Repository.Settings.value.snapshots_location)
            snapshot_copy_path = os.path.join(snapshots_location, filename)
            source_path = self.file.get_path()
            creation_timestamp = os.stat(source_path).st_ctime
            creation_date = datetime.fromtimestamp(creation_timestamp)
            if os.path.exists(snapshot_copy_path):
                raise ValueError("Snapshot with the same filename exists")
            total_size = os.path.getsize(source_path)
            copied_size = 0
            buffer_size = 1024 * 1024  # 1 MB
            with open(source_path, 'rb') as src, open(snapshot_copy_path, 'xb') as dst:
                copy_created = True
                while True:
                    buf = src.read(buffer_size)
                    if not buf:
                        break
                    dst.write(buf)
                    copied_size += len(buf)
                    self._update_progress(progress=copied_size / total_size)
            self.multistage_process.snapshot = Snapshot(filename=filename, date=creation_date, from_file=True)
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            if copy_created and snapshot_copy_path and os.path.exists(snapshot_copy_path):
                try:
                    os.remove(snapshot_copy_path)
                except OSError as cleanup_error:
                    print(f"Failed to remove partial snapshot {snapshot_copy_path}: {cleanup_error}")
            print(f"Error while copying snapshot file: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

@final
class SnapshotInstallationStepGenerateSnapshot(MultiStageProcessStage):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Generate snapshot", description="Fetches latest portage snapshot tree", multistage_process=multistage_process)
    def start(self):
        super().start()
        if self.multistage_process.reused_snapshot:
            self.complete(MultiStageProcessStageState.COMPLETED)
            return
        try:
            snapshot_path: str | None = None
            def catalyst_snapshot_handler(line: str) -> float | None:
                nonlocal snapshot_path
                prefix = "NOTICE:catalyst:Wrote snapshot to "
                if line.startswith(prefix):
                    snapshot_path = line[len(prefix):].strip()
                return None
            if not self.run_command_in_toolset(command="catalyst -s stable", progress_handler=catalyst_snapshot_handler):
                raise RuntimeError("Catalyst -s stable failed")
            if not snapshot_path:
                raise RuntimeError("Didn't find generated snapshot")
            filename = os.path.basename(snapshot_path)
            self.multistage_process.snapshot = Snapshot(filename=filename, date=datetime.now())
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during snapshot generation: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

@final
class SnapshotInstallationStepSetupPermissions(MultiStageProcessStage):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Setup permissions", description="Sets generated snapshot file permissions", multistage_process=multistage_process)
    def start(self):
        super().start()
        try:
            if self.multistage_process.snapshot is None:
                raise RuntimeError("Unknown spanshot")
            snapshots_location = resolve_path(Repository.Settings.value.snapshots_location)
            snapshot_real_path = os.path.join(snapshots_location, self.multistage_process.snapshot.filename)
            base_name, _ = os.path.splitext(self.multistage_process.snapshot.filename)
            lock_filename = base_name + ".lock"
            lock_file_path = os.path.join(snapshots_location, lock_filename)
            delete_file(file_path=lock_file_path, root_dir=snapshots_location)
            unlock_file_access(snapshot_real_path)
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during snapshot generation: {e}")
            self.complete(MultiStageProcessStageState.FAILED)
    def cleanup(self) -> bool:
        if not super().cleanup():
            return False
        # Remove file if installation failed
        if (self.multistage_process.status == MultiStageProcessState.FAILED and self.multistage_process.snapshot
                and not self.multistage_process.reused_snapshot):
            snapshots_location = resolve_path(Repository.Settings.value.snapshots_location)
            snapshot_real_path = os.path.join(snapshots_location, self.multistage_process.snapshot.filename)
            delete_file(file_path=snapshot_real_path, root_dir=snapshots_location)
        return True

@final
class SnapshotInstallationStepAnalyze(MultiStageProcessStage):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Analyze snapshots", description="Read metadata from snapshot", multistage_process=multistage_process)
    def start(self):
        super().start()
        try:
            if self.multistage_process.snapshot is None:
                raise RuntimeError("Unknown spanshot")
            snapshots_location = resolve_path(Repository.Settings.value.snapshots_location)
            snapshot_real_path = os.path.join(snapshots_location, self.multistage_process.snapshot.filename)
            # Mount snapshot and read timestamp from it.
            output = subprocess.check_output(['unsquashfs', '-cat', snapshot_real_path, "metadata/timestamp.chk"], text=True)
            try:
                self.multistage_process.snapshot.date = parse_strict_rfc_datetime(output.strip())
            except Exception as e:
                print(e)
                self.multistage_process.snapshot.date = None
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during snapshot generation: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

# ------------------------------------------------------------------------------
# Helper functions.
# ------------------------------------------------------------------------------

@root_function
def unlock_file_access(path: str):
    os.chmod(path, 0o777)

@root_function
def delete_file(file_path: str, root_dir: str):
    if not root_dir.strip():
        print("Root directory path cannot be empty")
        return
    file_path = os.path.abspath(file_path)
    root_dir = os.path.abspath(root_dir)
    # Check if file_path is inside root_dir
    if not file_path.startswith(root_dir + os.sep):
        print(f"File '{file_path}' is not inside root directory '{root_dir}'")
        return
    # Check if it's a regular file
    if not os.path.isfile(file_path):
        print(f"'{file_path}' is not a regular file")
        return
    # Delete the file
    os.remove(file_path)

def _local_for_user_files(proxy, path_argument: str):
    """Snapshot files generated in rootless toolset are owned by user, they don't need root helper."""
    from .root_function import ROOT_FUNCTION_REGISTRY
    original = ROOT_FUNCTION_REGISTRY[proxy.__name__]
    def call(*args, **kwargs):
        path = kwargs.get(path_argument, args[0] if args else None)
        if path and (not os.path.lexists(path) or os.lstat(path).st_uid == os.getuid()):
            return original(*args, **kwargs)
        return proxy(*args, **kwargs)
    return call

unlock_file_access = _local_for_user_files(unlock_file_access, path_argument="path")
delete_file = _local_for_user_files(delete_file, path_argument="file_path")

