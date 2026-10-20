from __future__ import annotations
import os, threading
from .repository import Repository, library_path, resolve_path
from .helper_functions import path_is_within
from .build_machine import VMRunner
from .lima import run_limactl, remove_empty_runner_temporary_directory

class VMRunnerManager:
    _instance = None

    # Note: To get machine list use Repository.Runner.value

    @classmethod
    def shared(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def add_runner(self, machine: VMRunner):
        Repository.Runner.value = [
            item for item in Repository.Runner.value if item.id != machine.id
        ] + [machine]

    def remove_runner(self, machine: VMRunner):
        """Removes machine and deletes its Lima instance with its disk (in background). Toolsets linked to it are
        moved to this computer."""
        Repository.Runner.value.remove(machine)
        settings = Repository.Settings.value
        if settings.default_runner_id == str(machine.id):
            settings.default_runner_id = None
        try:
            os.remove(os.path.join(machine.machine_directory, "runner.json"))
        except FileNotFoundError:
            pass
        def delete():
            run_limactl(["delete", "--force", machine.instance_name], print, home=machine.lima_home)
            machine.delete_swap_disk()
            try:
                os.remove(machine.workspace_image_path)
            except FileNotFoundError:
                pass
            remove_empty_runner_temporary_directory(machine.instance_name)
        threading.Thread(target=delete, daemon=True).start()

    def is_name_available(self, name: str, machine: VMRunner | None = None) -> bool:
        """Name is not empty and not used by other machine than given one."""
        name = name.strip()
        return bool(name) and all(item.name != name for item in Repository.Runner.value if item is not machine)

def shared_paths(additional_paths: list[str] | None = None) -> list[str]:
    """Managed data folders and operation-specific folders shared with machines."""
    locations = [
        library_path("Toolsets"), library_path("Snapshots"), library_path("Releng"),
        library_path("Overlays"), library_path("Projects"), library_path("Builds"),
        library_path("Cache"), library_path("Temporary"), library_path("Packages"),
    ]
    paths = []
    locations += [os.path.abspath(os.path.expanduser(path)) for path in (additional_paths or [])]
    for location in sorted({resolve_path(location) for location in locations}, key=len):
        if not any(path_is_within(location, path) for path in paths):
            paths.append(location)
    for path in paths:
        os.makedirs(path, exist_ok=True)
    return paths
