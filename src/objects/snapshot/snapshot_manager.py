from typing import final
from datetime import datetime
from .repository import Repository
import os, subprocess
from .snapshot import Snapshot
from .helper_functions import parse_strict_rfc_datetime

def sorted_snapshots(snapshots: list[Snapshot]) -> list[Snapshot]:
    """Newest snapshots first, snapshots without date last."""
    return sorted(snapshots, key=lambda snapshot: snapshot.date.timestamp() if snapshot.date else 0, reverse=True)

@final
class SnapshotManager:
    _instance = None

    # Note: To get spanshots list use Repository.Snapshot.value

    @classmethod
    def shared(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def refresh(self):
        snapshots = Repository.Snapshot.value
        # Detect missing snapshots and add them to repository without date
        snapshots_location = os.path.realpath(os.path.expanduser(Repository.Settings.value.snapshots_location))
        # --- Step 1: Scan directory for existing .sqfs files ---
        if not os.path.isdir(snapshots_location):
            os.makedirs(snapshots_location, exist_ok=True)
        found_filenames = {
            f for f in os.listdir(snapshots_location)
            if f.endswith(".sqfs") and os.path.isfile(os.path.join(snapshots_location, f))
        }
        # --- Step 2: Check for new snapshots not in repository ---
        existing_filenames = {snapshot.filename for snapshot in snapshots}
        missing_files = found_filenames - existing_filenames
        for filename in missing_files:
            full_path = os.path.join(snapshots_location, filename)
            try:
                output = subprocess.check_output(['unsquashfs', '-cat', full_path, "metadata/timestamp.chk"], text=True)
                try:
                    timestamp = parse_strict_rfc_datetime(output.strip())
                except Exception as e:
                    print(e)
                    timestamp = None
                self.add_snapshot(Snapshot(filename=filename, date=timestamp))
            except Exception as e:
                print(f"Error reading {full_path}: {e}")
        # --- Step 3: Remove records for deleted snapshot files ---
        deleted_snapshots = [snapshot for snapshot in snapshots if snapshot.filename not in found_filenames]
        for snapshot in deleted_snapshots:
            self.remove_snapshot(snapshot)
        # --- Step 4: Keep newest snapshots first ---
        ordered = sorted_snapshots(Repository.Snapshot.value)
        if ordered != Repository.Snapshot.value:
            Repository.Snapshot.value = ordered

    def add_snapshot(self, snapshot: Snapshot):
        # Remove existing snapshot with the same filename
        Repository.Snapshot.value = sorted_snapshots(
            [s for s in Repository.Snapshot.value if s.filename != snapshot.filename] + [snapshot]
        )

    # Auto clean:

    def start_auto_clean(self):
        """Removes unused snapshots when enabled in settings, whenever snapshots, projects or running processes change."""
        from .settings import SettingsEvents
        from .repository import RepositoryEvent
        from .multistage_process import MultiStageProcess, MultiStageProcessEvent
        self._auto_clean_scheduled = False
        # Event buses keep weak references, bound method of shared manager stays alive (lambda wouldn't).
        Repository.Settings.value.event_bus.subscribe(SettingsEvents.AUTO_CLEAN_SNAPSHOTS_CHANGED, self.schedule_auto_clean)
        Repository.Snapshot.event_bus.subscribe(RepositoryEvent.VALUE_CHANGED, self.schedule_auto_clean)
        Repository.ProjectDirectory.event_bus.subscribe(RepositoryEvent.VALUE_CHANGED, self.schedule_auto_clean)
        MultiStageProcess.event_bus.subscribe(MultiStageProcessEvent.STARTED_PROCESSES_CHANGED, self.schedule_auto_clean)
        self.schedule_auto_clean()

    def schedule_auto_clean(self, *args):
        if getattr(self, "_auto_clean_scheduled", False) or not Repository.Settings.value.auto_clean_snapshots:
            return
        from gi.repository import GLib
        self._auto_clean_scheduled = True
        def clean():
            self._auto_clean_scheduled = False
            self.auto_clean()
            return False
        GLib.idle_add(clean)

    def unused_snapshots(self) -> list[Snapshot]:
        """Snapshots removed by auto clean: not used by projects, builds that can be deployed (their repository can be
        installed with them) or running processes, not newest, and not added from file (these can contain own
        changes)."""
        from .multistage_process import MultiStageProcess, MultiStageProcessState
        snapshots = sorted_snapshots(Repository.Snapshot.value)
        used = {snapshot.filename for snapshot in snapshots[:1]}
        for project in Repository.ProjectDirectory.value:
            if snapshot := project.get_snapshot():
                used.add(snapshot.filename)
            used |= _deployable_builds_snapshots(project)
        for process in MultiStageProcess.started_processes:
            snapshot = getattr(process, "snapshot", None)
            if process.status == MultiStageProcessState.IN_PROGRESS and isinstance(snapshot, Snapshot):
                used.add(snapshot.filename)
        return [snapshot for snapshot in snapshots
                if snapshot.filename not in used and not snapshot.may_contain_own_changes]

    def auto_clean(self):
        if not Repository.Settings.value.auto_clean_snapshots:
            return
        for snapshot in self.unused_snapshots():
            print(f"Removing unused snapshot {snapshot.filename}")
            try:
                self.remove_snapshot(snapshot)
            except Exception as e:
                print(f"Failed to remove snapshot {snapshot.filename}: {e}")

    def remove_snapshot(self, snapshot: Snapshot):
        from .rootless import remove_extracted_squashfs, MachineExecutor
        remove_extracted_squashfs(snapshot.file_path(), kind="snapshots")
        # Snapshots are extracted in machines that built with them, running ones are cleaned too.
        for machine in Repository.BuildMachine.value:
            if machine.is_running:
                remove_extracted_squashfs(snapshot.file_path(), kind="snapshots", executor=MachineExecutor(machine))
        if os.path.isfile(snapshot.file_path()):
            os.remove(snapshot.file_path())
        Repository.Snapshot.value.remove(snapshot)


def _deployable_builds_snapshots(project) -> set[str]:
    """Snapshots used by completed builds of project that can be deployed (stage3, stage4, live CD)."""
    from .project_build import load_project_builds
    from .deploy_installation import is_deployable
    from .deploy_live_iso import is_live_iso_build
    files = set()
    try:
        for build in load_project_builds(project):
            if is_deployable(project, build) or is_live_iso_build(build):
                if name := (((build.details or {}).get("inputs") or {}).get("snapshot") or {}).get("file"):
                    files.add(name)
    except Exception as e:
        print(f"Failed to read builds of {project.name}: {e}")
    return files
