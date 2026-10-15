from .repository import Repository
import os, shutil, uuid, subprocess, json
from .toolset import Toolset, ToolsetEnv

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
        toolsets_location = os.path.realpath(os.path.expanduser(Repository.Settings.value.toolsets_location))
        # --- Step 1: Scan directory for existing .sqfs files ---
        if not os.path.isdir(toolsets_location):
            os.makedirs(toolsets_location, exist_ok=True)
        found_filenames = {
            f for f in os.listdir(toolsets_location)
            if f.endswith(".sqfs") and os.path.isfile(os.path.join(toolsets_location, f))
        }
        # --- Step 2: Check for new toolsets not in repository ---
        existing_filenames = {toolset.filename for toolset in toolsets}
        missing_files = found_filenames - existing_filenames
        for filename in missing_files:
            full_path = os.path.join(toolsets_location, filename)
            # Load metadata from json file:
            try:
                output = subprocess.check_output(['unsquashfs', '-cat', full_path, "toolset.json"], text=True)
                metadata = json.loads(output)
                toolset = Toolset(
                    env=ToolsetEnv.EXTERNAL,
                    uuid=uuid.uuid4(),
                    name=filename[:-5], # Removes .sqfs
                    metadata=metadata
                )
                self.add_toolset(toolset)
            except subprocess.CalledProcessError as e:
                print(f"Error reading {full_path}: {e}")
        # --- Step 3: Remove records for deleted toolset files ---
        deleted_toolsets = [toolset for toolset in toolsets if toolset.filename not in found_filenames]
        for toolset in deleted_toolsets:
            self.remove_toolset(toolset)

    def add_toolset(self, toolset: Toolset):
        # Remove existing toolset before adding.
        Repository.Toolset.value = [s for s in Repository.Toolset.value if s.uuid != toolset.uuid]
        Repository.Toolset.value.append(toolset)

    def remove_toolset(self, toolset: Toolset):
        from .rootless import remove_extracted_squashfs
        if toolset.machine is None or toolset.machine.is_running: # Stopped machine is not started only to clean up.
            remove_extracted_squashfs(toolset.file_path(), kind="toolsets", executor=toolset.executor)
        if os.path.isfile(toolset.file_path()):
            os.remove(toolset.file_path())
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
        shutil.move(toolset.file_path(), new_path)
        toolset.name = name
        Repository.Toolset.save()

