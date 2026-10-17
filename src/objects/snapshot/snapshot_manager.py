from typing import final
from .repository import Repository
import os
from .snapshot import Snapshot

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
        if os.path.isdir(Snapshot.base_location()):
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
        from .rootless import remove_extracted_squashfs, MachineExecutor
        remove_extracted_squashfs(snapshot.file_path(), kind="snapshots")
        # Snapshots are extracted in machines that built with them, running ones are cleaned too.
        for machine in Repository.BuildMachine.value:
            if machine.is_running:
                remove_extracted_squashfs(snapshot.file_path(), kind="snapshots", executor=MachineExecutor(machine))
        if os.path.isfile(snapshot.file_path()):
            os.remove(snapshot.file_path())
        marker = snapshot.file_path() + ".json"
        if os.path.isfile(marker):
            os.remove(marker)
        Repository.Snapshot.value.remove(snapshot)
