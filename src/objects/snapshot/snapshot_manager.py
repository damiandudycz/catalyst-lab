from typing import final
from .repository import Repository
import os
from .snapshot import Snapshot, sorted_snapshots

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
        Repository.Snapshot.value = Snapshot.load_all()

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
        # Meson installs these modules flat inside the catalystlab package.
        from .qemu_run import QemuRun, QemuRunEvent
        self._auto_clean_scheduled = False
        # Event buses keep weak references, bound method of shared manager stays alive (lambda wouldn't).
        Repository.Settings.value.event_bus.subscribe(SettingsEvents.AUTO_CLEAN_SNAPSHOTS_CHANGED, self.schedule_auto_clean)
        Repository.Snapshot.event_bus.subscribe(RepositoryEvent.VALUE_CHANGED, self.schedule_auto_clean)
        Repository.ProjectDirectory.event_bus.subscribe(RepositoryEvent.VALUE_CHANGED, self.schedule_auto_clean)
        MultiStageProcess.event_bus.subscribe(MultiStageProcessEvent.STARTED_PROCESSES_CHANGED, self.schedule_auto_clean)
        QemuRun.running_runs_event_bus.subscribe(QemuRunEvent.RUNNING_RUNS_CHANGED, self.schedule_auto_clean)
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
        """Remove snapshots unless selected by a project, newest, or currently in use.
        Completed build references do not retain snapshots.
        """
        from .multistage_process import MultiStageProcess, MultiStageProcessState
        snapshots = sorted_snapshots(Repository.Snapshot.value)
        used = {snapshot.filename for snapshot in snapshots[:1]}
        for project in Repository.ProjectDirectory.value:
            if snapshot := project.get_snapshot():
                used.add(snapshot.filename)
        for process in MultiStageProcess.started_processes:
            snapshot = getattr(process, "snapshot", None)
            if process.status == MultiStageProcessState.IN_PROGRESS and isinstance(snapshot, Snapshot):
                used.add(snapshot.filename)
        # A QEMU run can compile a custom kernel from a repository snapshot.
        # Keep it out of the unused set so guarded deletion and auto-cleanup
        # protect it while the VM build is active.
        from .qemu_run import QemuRun
        for run in QemuRun.running_runs:
            snapshot = getattr(getattr(run, "support", None), "kernel_snapshot", None)
            if run.is_running and isinstance(snapshot, Snapshot):
                used.add(snapshot.filename)
        return [snapshot for snapshot in snapshots
                if snapshot.filename not in used]

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
        from .rootless import remove_extracted_squashfs
        remove_extracted_squashfs(snapshot.file_path(), kind="snapshots")
        if os.path.isfile(snapshot.file_path()):
            os.remove(snapshot.file_path())
        marker = snapshot.file_path() + ".json"
        if os.path.isfile(marker):
            os.remove(marker)
        Repository.Snapshot.value.remove(snapshot)
