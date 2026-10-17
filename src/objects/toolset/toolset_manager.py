from .repository import Repository, resolve_path
import os, shutil
from .toolset import Toolset

class ToolsetManager:
    _instance = None

    # Note: To get toolset list use Repository.Toolset.value

    @classmethod
    def shared(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def refresh(self):
        toolsets = Repository.Toolset.value
        # Detect missing toolsets and add them to repository
        toolsets_location = resolve_path(Repository.Settings.value.toolsets_location)
        if not os.path.isdir(toolsets_location):
            return
        found_names = {entry.name for entry in os.scandir(toolsets_location)
                       if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".")
                       and os.path.isfile(os.path.join(entry.path, "toolset.sqfs"))}
        deleted_toolsets = [toolset for toolset in toolsets if Toolset.sanitized_name_for_name(toolset.name) not in found_names]
        for toolset in deleted_toolsets:
            self.remove_toolset(toolset)

    def add_toolset(self, toolset: Toolset):
        Repository.Toolset.value = [
            item for item in Repository.Toolset.value if item.uuid != toolset.uuid
        ] + [toolset]

    def remove_toolset(self, toolset: Toolset):
        from .rootless import remove_extracted_squashfs
        if toolset.machine is None or toolset.machine.is_running: # Stopped machine is not started only to clean up.
            remove_extracted_squashfs(toolset.file_path(), kind="toolsets", executor=toolset.executor)
        if os.path.isdir(os.path.dirname(toolset.file_path())):
            shutil.rmtree(os.path.dirname(toolset.file_path()))
        Repository.Toolset.value.remove(toolset)

    def is_name_available(self, name: str) -> bool:
        if not name:
            return False
        file_path = Toolset.file_path_for_name(name=name)
        return not os.path.exists(file_path)

    def rename_toolset(self, toolset: Toolset, name: str):
        if not self.is_name_available(name=name):
            raise RuntimeError(f"Toolset name {name} is not available")
        new_path = Toolset.file_path_for_name(name=name)
        from .rootless import remove_extracted_squashfs
        if toolset.machine is None or toolset.machine.is_running: # Extracted again for new file name when needed.
            remove_extracted_squashfs(toolset.file_path(), kind="toolsets", executor=toolset.executor)
        shutil.move(os.path.dirname(toolset.file_path()), os.path.dirname(new_path))
        toolset.name = name
        Repository.Toolset.save()

