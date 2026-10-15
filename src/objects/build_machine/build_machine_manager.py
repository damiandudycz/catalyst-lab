from __future__ import annotations
import os, threading
from .repository import Repository, library_directory
from .build_machine import BuildMachine
from .lima import run_limactl

class BuildMachineManager:
    _instance = None

    # Note: To get machine list use Repository.BuildMachine.value

    @classmethod
    def shared(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def add_machine(self, machine: BuildMachine):
        Repository.BuildMachine.value = [item for item in Repository.BuildMachine.value if item.id != machine.id]
        Repository.BuildMachine.value.append(machine)

    def remove_machine(self, machine: BuildMachine):
        """Removes machine and deletes its Lima instance with its disk (in background). Toolsets linked to it are
        moved to this computer."""
        Repository.BuildMachine.value.remove(machine)
        for toolset in Repository.Toolset.value:
            if getattr(toolset, "machine_id", None) == machine.id:
                toolset.machine_id = None
        Repository.Toolset.save()
        def delete():
            run_limactl(["delete", "--force", machine.instance_name], print, home=machine.lima_home)
            machine.delete_swap_disk()
        threading.Thread(target=delete, daemon=True).start()

    def is_name_available(self, name: str, machine: BuildMachine | None = None) -> bool:
        """Name is not empty and not used by other machine than given one."""
        name = name.strip()
        return bool(name) and all(item.name != name for item in Repository.BuildMachine.value if item is not machine)

def shared_paths() -> list[str]:
    """Folders of Catalyst Lab shared with machines, mounted at the same paths. Locations inside other ones are skipped."""
    settings = Repository.Settings.value
    locations = [
        library_directory(), settings.toolsets_location, settings.snapshots_location, settings.releng_location,
        settings.overlay_location, settings.project_location, settings.builds_location, settings.cache_location,
        settings.temporary_location, settings.packages_location
    ]
    paths = []
    for location in sorted({os.path.realpath(os.path.expanduser(location)) for location in locations}, key=len):
        if not any(location == path or location.startswith(path + os.sep) for path in paths):
            paths.append(location)
    for path in paths:
        os.makedirs(path, exist_ok=True)
    return paths
