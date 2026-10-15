import os, shutil, subprocess, tempfile
from gi.repository import Gio
from enum import Enum, auto
from dataclasses import dataclass
from .runtime_env import RuntimeEnv

class HotFix(Enum):
    SNAKEOIL_NAMESPACES_FAKE = auto()
    DEFAULT_NAMESERVERS = auto()

    @classmethod
    @property
    def catalyst_fixes(cls):
        return [HotFix.SNAKEOIL_NAMESPACES_FAKE]

    @property
    def get_patch_spec(self):
        match self:
            case HotFix.SNAKEOIL_NAMESPACES_FAKE:
                return PatchSpec( # This patch fakes unshare call making it possible to run catalyst inside isolated env.
                            source_path="/usr/lib/python3.12/site-packages/snakeoil/process/namespaces.py",
                            patch_filename="namespaces.patch"  # The patch file inside patches/
                        )
            case HotFix.DEFAULT_NAMESERVERS:
                return PatchSpec( # Adds default Google nameservers
                            source_path="/etc/resolv.conf",
                            patch_filename="default-resolv-conf.patch"
                        )

@dataclass
class PatchSpec:
    source_path: str    # Path to the file to be patched
    patch_filename: str # The name of the patch file (located in resources/patches/)

# TODO: Take path for patch_spec from used toolset and not always from main system
def apply_patch_and_store_for_isolated_system(runtime_env: RuntimeEnv, toolset_root: str, storage_root: str, patch_spec: PatchSpec) -> str | None:
    """
    Applies a patch file (from project resources) to a copy of the original file
    and stores the result in <storage_root>/<original_relative_path>.
    Returns the path to the patched file.
    """
    # Resolve the original file's full path using runtime_env and toolset_root
    original_path = runtime_env.resolve_path_for_host_access((toolset_root + "/" + patch_spec.source_path).replace("//", "/"))

    # Load patch content from GResource
    resource_path = f"/com/damiandudycz/CatalystLab/patches/{patch_spec.patch_filename}"

    try:
        gfile = Gio.File.new_for_uri(f"resource://{resource_path}")
        content_bytes = gfile.load_contents(None)[1]
        patch_contents = content_bytes.decode("utf-8")
    except Exception as e:
        raise FileNotFoundError(f"Failed to load patch from resource '{resource_path}': {e}")

    # If file is not found in toolset_root, skip patching and return None
    if not os.path.isfile(original_path):
        print(f"Original file not found: {original_path}")
        print("Patching skipped")
        return None

    # Compute target path for the patched version
    relative_path = os.path.relpath(patch_spec.source_path, "/")
    target_path = os.path.join(storage_root, relative_path)
    target_dir = os.path.dirname(target_path)
    os.makedirs(target_dir, exist_ok=True)

    # Copy the original file to the temporary location
    shutil.copy2(original_path, target_path)

    # Write the patch to a temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as temp_patch_file:
        temp_patch_file.write(patch_contents)
        temp_patch_path = temp_patch_file.name

    # Apply the patch
    result = subprocess.run(
        ["patch", target_path, temp_patch_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    # Clean up the temp patch file
    os.remove(temp_patch_path)

    if result.returncode != 0:
        raise RuntimeError(f"Failed to apply patch:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    return target_path

