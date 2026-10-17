from __future__ import annotations
import os, threading
from .repository import Repository, library_path, resolve_path
from .helper_functions import path_is_within
from .build_machine import BuildMachine
from .lima import run_limactl, remove_empty_build_machine_temporary_directory

class BuildMachineManager:
    _instance = None

    # Note: To get machine list use Repository.BuildMachine.value

    @classmethod
    def shared(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def add_machine(self, machine: BuildMachine):
        Repository.BuildMachine.value = [
            item for item in Repository.BuildMachine.value if item.id != machine.id
        ] + [machine]

    def remove_machine(self, machine: BuildMachine):
        """Removes machine and deletes its Lima instance with its disk (in background). Toolsets linked to it are
        moved to this computer."""
        Repository.BuildMachine.value.remove(machine)
        metadata_path = os.path.join(machine.machine_directory, "catalystlab.json")
        try:
            os.remove(metadata_path)
        except FileNotFoundError:
            pass
        for toolset in Repository.Toolset.value:
            if getattr(toolset, "machine_id", None) == machine.id:
                toolset.machine_id = None
        Repository.Toolset.save()
        def delete():
            run_limactl(["delete", "--force", machine.instance_name], print, home=machine.lima_home)
            machine.delete_swap_disk()
            try:
                os.remove(machine.workspace_image_path)
            except FileNotFoundError:
                pass
            remove_empty_build_machine_temporary_directory(machine.instance_name)
        threading.Thread(target=delete, daemon=True).start()

    def is_name_available(self, name: str, machine: BuildMachine | None = None) -> bool:
        """Name is not empty and not used by other machine than given one."""
        name = name.strip()
        return bool(name) and all(item.name != name for item in Repository.BuildMachine.value if item is not machine)

def shared_paths() -> list[str]:
    """Managed data folders shared with machines, all derived from the Library root."""
    locations = [
        library_path("Toolsets"), library_path("Snapshots"), library_path("Releng"),
        library_path("Overlays"), library_path("Projects"), library_path("Builds"),
        library_path("Cache"), library_path("Temporary"), library_path("Packages"),
    ]
    paths = []
    for location in sorted({resolve_path(location) for location in locations}, key=len):
        if not any(path_is_within(location, path) for path in paths):
            paths.append(location)
    for path in paths:
        os.makedirs(path, exist_ok=True)
    return paths
