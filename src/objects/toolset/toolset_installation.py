from __future__ import annotations
import os, uuid, shutil, re, requests, time
from typing import final
from gi.repository import Gio
from .root_function import root_function, local_for_rootless_paths
from .root_helper_server import ServerResponse, ServerResponseStatusCode
from .toolset import Toolset, ToolsetEnv
from .helper_functions import create_work_directory, delete_work_directory, extract
from .rootless import rootless_toolset_unsupported_reason, extract_tarball, executor_for_machine, toolset_stages_directory
from .rootless import create_work_directory as create_rootless_work_directory
from .toolset_manager import ToolsetManager
from .toolset_application import order_selected_applications

from .multistage_process import (
    MultiStageProcess, MultiStageProcessStage,
    MultiStageProcessStageState
)

_WEBRSYNC_PROGRESS = re.compile(
    r"\s*\d+[KMGTP]?\s+(?:\.{1,10}\s*)+(\d{1,3})%\s+\d+(?:\.\d+)?[KMGTP]?(?:[= ]\d+(?:\.\d+)?s?)?"
)

def _emerge_progress(line: str) -> float | None:
    match = re.match(r"^>>> Completed \((\d+) of (\d+)\)", line)
    if not match:
        return None
    completed, total = map(int, match.groups())
    return completed / total if total else None

def _webrsync_progress(line: str) -> float | None:
    match = _WEBRSYNC_PROGRESS.match(line)
    return int(match.group(1)) / 100 if match else None

# ------------------------------------------------------------------------------
# Toolset installation.
# ------------------------------------------------------------------------------

@final
class ToolsetInstallation(MultiStageProcess):
    """Handles the full toolset installation lifecycle."""
    def __init__(self, alias: str, stage_url: ParseResult, allow_binpkgs: bool, apps_selection: list[ToolsetApplicationSelection], machine=None):
        self.alias = alias
        self.stage_url = stage_url
        self.allow_binpkgs = allow_binpkgs
        self.apps_selection = order_selected_applications(apps_selection)
        # Toolset is installed in virtual machine when given, or on this computer. It's installed without root
        # privileges (in user namespace) in machines and when this computer supports it.
        self.machine = machine
        self.executor = executor_for_machine(machine)
        self.rootless = machine is not None or rootless_toolset_unsupported_reason() is None
        super().__init__(title="Toolset installation")

    def setup_stages(self):
        self.stages.append(ToolsetInstallationStepDownload(url=self.stage_url, multistage_process=self))
        self.stages.append(ToolsetInstallationStepExtract(multistage_process=self))
        self.stages.append(ToolsetInstallationStepSpawn(multistage_process=self))
        self.stages.append(ToolsetInstallationStepUpdatePortage(multistage_process=self))
        for app_selection in self.apps_selection:
            self.stages.append(ToolsetInstallationStepInstallApp(app_selection=app_selection, multistage_process=self))
        self.stages.append(ToolsetInstallationStepVerify(multistage_process=self))
        self.stages.append(ToolsetInstallationStepCompress(multistage_process=self))

    def complete_process(self, success: bool):
        if success:
            # Update version_id of selected apps
            for app_selection in self.apps_selection:
                self.toolset.metadata.setdefault(app_selection.app.package, {})["version_id"] = str(app_selection.version.id)
            ToolsetManager.shared().add_toolset(self.toolset)

    def name(self) -> str:
        return self.alias

# ------------------------------------------------------------------------------
# Installation process steps.
# ------------------------------------------------------------------------------

class ToolsetInstallationStep(MultiStageProcessStage):
    def start(self):
        self.namespace_processes = [] # Rootless processes, terminated when canceled.
        super().start()
    def cancel(self):
        super().cancel()
        for process in getattr(self, "namespace_processes", []):
            process.terminate()
# Steps implementations:

@final
class ToolsetInstallationStepDownload(ToolsetInstallationStep):
    def __init__(self, url: ParseResult, multistage_process: MultiStageProcess):
        super().__init__(name="Download stage tarball", description="Downloading Gentoo stage tarball", multistage_process=multistage_process)
        self.url = url
    def start(self):
        super().start()
        partial_path = None
        try:
            cache_directory = toolset_stages_directory()
            os.makedirs(cache_directory, exist_ok=True)
            filename = os.path.basename(self.url.path)
            cached_path = os.path.join(cache_directory, filename)
            self.multistage_process.stage_tarball_path = cached_path
            if os.path.isfile(cached_path):
                self.log(f"Using cached stage archive {filename}")
                self.complete(MultiStageProcessStageState.COMPLETED)
                return

            partial_path = os.path.join(cache_directory, f".{filename}.{uuid.uuid4().hex}.part")
            with requests.get(self.url.geturl(), stream=True, timeout=10) as response:
                response.raise_for_status()
                total_size = int(response.headers.get('content-length', 0))
                downloaded = 0
                with open(partial_path, "wb") as stage_file:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if self._cancel_event.is_set():
                            return
                        if chunk:
                            stage_file.write(chunk)
                            downloaded += len(chunk)
                            if total_size:
                                self._update_progress(downloaded / total_size)
            os.replace(partial_path, cached_path)
            partial_path = None
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during download: {e}")
            self.complete(MultiStageProcessStageState.FAILED)
        finally:
            if partial_path and os.path.exists(partial_path):
                os.remove(partial_path)
@final
class ToolsetInstallationStepExtract(ToolsetInstallationStep):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Extract stage tarball", description="Extracts Gentoo stage tarball to work directory", multistage_process=multistage_process)
    def start(self):
        super().start()
        try:
            prefix = f"toolsets/{Toolset.sanitized_name_for_name(name=self.multistage_process.alias)}/setup_"
            if self.multistage_process.machine:
                self.workspace_user = f"Installation of toolset {self.multistage_process.alias}"
                self.multistage_process.machine.acquire_workspace(self.log, self.namespace_processes, user=self.workspace_user)
                self.workspace_acquired = True
                self.multistage_process.tmp_stage_extract_dir = create_rootless_work_directory(prefix=prefix, executor=self.multistage_process.executor)
            else:
                self.multistage_process.tmp_stage_extract_dir = create_work_directory(prefix=prefix, rootless=self.multistage_process.rootless)
            return_value = False
            def completion_handler(response: ServerResponse):
                nonlocal return_value
                return_value = response.code == ServerResponseStatusCode.OK
            def output_handler(output_line: str):
                if output_line.startswith("PROGRESS: "):
                    try:
                        progress_str = output_line[len("PROGRESS: "):]
                        progress_value = float(progress_str)
                        self._update_progress(progress_value)
                    except ValueError:
                        pass
            if self.multistage_process.rootless:
                return_value = extract_tarball(
                    tarball=self.multistage_process.stage_tarball_path,
                    directory=self.multistage_process.tmp_stage_extract_dir,
                    output_handler=output_handler,
                    process_holder=self.namespace_processes,
                    executor=self.multistage_process.executor
                )
            else:
                self.server_call = extract._async_raw(
                    handler=output_handler,
                    completion_handler=completion_handler,
                    tarball=self.multistage_process.stage_tarball_path,
                    directory=self.multistage_process.tmp_stage_extract_dir
                )
                self.server_call.thread.join()
            if not self._cancel_event.is_set():
                self.server_call = None
                self.complete(MultiStageProcessStageState.COMPLETED if return_value else MultiStageProcessStageState.FAILED)
        except Exception as e:
            print(f"Error extracting stage tarball: {e}")
            self.complete(MultiStageProcessStageState.FAILED)
    def cleanup(self) -> bool:
        if not super().cleanup():
            return False
        if getattr(self.multistage_process, "tmp_stage_extract_dir", None) and not self.multistage_process.machine:
            delete_work_directory(self.multistage_process.tmp_stage_extract_dir)
        # Working space of machine is deleted with all its files.
        if getattr(self, "workspace_acquired", False):
            self.multistage_process.machine.release_workspace(user=self.workspace_user)
            self.workspace_acquired = False
        return True

@final
class ToolsetInstallationStepSpawn(ToolsetInstallationStep):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Create environment", description="Prepares Gentoo environment for work", multistage_process=multistage_process)
    def start(self):
        super().start()
        try:
            toolset_name = self.multistage_process.name()
            self.multistage_process.toolset = Toolset(ToolsetEnv.EXTERNAL, uuid.uuid4(), toolset_name, squashfs_binding_dir=self.multistage_process.tmp_stage_extract_dir,
                machine_id=self.multistage_process.machine.id if self.multistage_process.machine else None)
            now = int(time.time())
            self.multistage_process.toolset.metadata['date_created'] = now
            self.multistage_process.toolset.metadata['date_updated'] = now
            self.multistage_process.toolset.metadata['source'] = self.multistage_process.stage_url.geturl()
            self.multistage_process.toolset.metadata['allow_binpkgs'] = self.multistage_process.allow_binpkgs
            if not self.multistage_process.toolset.reserve():
                raise RuntimeError("Failed to reserve toolset")
            self.multistage_process.toolset.spawn(store_changes=True)
            commands = [
                "env-update && source /etc/profile",
                "getuto"
            ]
            for i, command in enumerate(commands):
                if self._cancel_event.is_set():
                    return
                result = self.run_command_in_toolset(command=command)
                if not result:
                    raise RuntimeError(f"Command {command} failed")
                self._update_progress((i + 1) / len(commands))
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error spawning temporary toolset: {e}")
            self.complete(MultiStageProcessStageState.FAILED)
    def cleanup(self) -> bool:
        if not super().cleanup():
            return False
        if getattr(self.multistage_process, 'toolset', None):
            if self.multistage_process.toolset.spawned:
                self.multistage_process.toolset.unspawn(rebuild_squashfs_if_needed=False)
            self.multistage_process.toolset.release()
            return True
        return False

@final
class ToolsetInstallationStepUpdatePortage(ToolsetInstallationStep):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Synchronize portage", description="Synchronizes portage tree", multistage_process=multistage_process)
    def start(self):
        super().start()
        try:
            result = self.run_command_in_toolset(command="emerge-webrsync", progress_handler=_webrsync_progress)
            self.complete(MultiStageProcessStageState.COMPLETED if result else MultiStageProcessStageState.FAILED)
        except Exception as e:
            print(f"Error synchronizing Portage: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

@final
class ToolsetInstallationStepInstallApp(ToolsetInstallationStep):
    def __init__(self, app_selection: ToolsetApplicationSelection, multistage_process: MultiStageProcess):
        super().__init__(name=f"Install {app_selection.app.name}", description=f"Emerges {app_selection.app.package} package", multistage_process=multistage_process)
        self.app_selection = app_selection
    def start(self):
        super().start()
        try:
            files = app_portage_files(self.app_selection)
            if files:
                self.multistage_process.toolset.write_portage_files(files)
            flags = "--getbinpkg --deep --update --changed-use" if self.multistage_process.allow_binpkgs else "--deep --update --changed-use"
            result = self.run_command_in_toolset(command=f"emerge {flags} {self.app_selection.app.package}", progress_handler=_emerge_progress)
            self.complete(MultiStageProcessStageState.COMPLETED if result else MultiStageProcessStageState.FAILED)
        except Exception as e:
            print(f"Error during app installation: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

@final
class ToolsetInstallationStepVerify(ToolsetInstallationStep):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Analyze toolset", description="Collects information about toolset", multistage_process=multistage_process)
    def start(self):
        super().start()
        try:
            analysis_result = self.multistage_process.toolset.analyze(save=True)
            self.multistage_process.toolset.write_metadata_to_json(metadata=analysis_result)
            self.complete(MultiStageProcessStageState.COMPLETED if analysis_result else MultiStageProcessStageState.FAILED)
        except Exception as e:
            print(f"Error during toolset verification: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

@final
class ToolsetInstallationStepCompress(ToolsetInstallationStep):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Compress", description="Compresses toolset into .squashfs file", multistage_process=multistage_process)
        self.squashfs_process = None
    def start(self):
        super().start()
        try:
            file_path = self.multistage_process.toolset.file_path()
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            # Build the image in Cache, which is shared with linked machines. Toolsets itself is not guaranteed to
            # be mounted inside the executor, and a fixed sibling .tmp also collides if two installs use one name.
            cache_directory = os.path.join(self.multistage_process.executor.cache_directory(), "ToolsetBuilds")
            os.makedirs(cache_directory, exist_ok=True)
            self.toolset_squashfs_file = os.path.join(
                cache_directory, f"toolset-{uuid.uuid4().hex}.sqfs.tmp"
            )
            self.squashfs_process = self.multistage_process.toolset.create_squashfs(output_file=self.toolset_squashfs_file)
            for line in self.squashfs_process.stdout:
                line = line.strip()
                if line.isdigit():
                    percent = int(line)
                    self._update_progress(percent / 100.0)
                elif line:
                    self.log(line)
            if self.squashfs_process.wait() != 0:
                raise RuntimeError("Failed to create squashfs file; see compressor output above")
            self.squashfs_process = None
            if not os.path.isfile(self.toolset_squashfs_file):
                try:
                    cache_entries = ", ".join(sorted(os.listdir(cache_directory))) or "(empty)"
                except OSError as error:
                    cache_entries = f"could not list directory: {error}"
                self.log(f"SquashFS output directory contents: {cache_entries}")
                raise FileNotFoundError(
                    f"SquashFS process completed without creating its output file: {self.toolset_squashfs_file}"
                )
            shutil.move(self.toolset_squashfs_file, file_path)
            self.toolset_squashfs_file = None
            self.multistage_process.toolset.unspawn(rebuild_squashfs_if_needed=False, clean_squashfs_binding_dir=False) # Need to unspawn now, to prevent issues with unmounting after squashfs_file was set
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during toolset compression: {e}")
            self.complete(MultiStageProcessStageState.FAILED)
    def cleanup(self) -> bool:
        if not super().cleanup():
            return False
        if self.state != MultiStageProcessStageState.COMPLETED:
            if getattr(self, "toolset_squashfs_file", None) and os.path.isfile(self.toolset_squashfs_file):
                os.remove(self.toolset_squashfs_file)
        return True
    def cancel(self):
        super().cancel()
        proc = self.squashfs_process
        if proc and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.squashfs_process = None

@root_function
def insert_portage_file(relative_path: str, content: str, toolset_root: str):
    """Writes file in /etc/portage of toolset."""
    path = os.path.join(toolset_root, "etc", "portage", relative_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

insert_portage_file = local_for_rootless_paths(insert_portage_file, path_argument="toolset_root")

def portage_config_path(config_dir: str, app_name: str) -> str:
    """Path of app configuration file, relative to /etc/portage."""
    return os.path.join(config_dir, app_name.replace("/", "_"))

def portage_patch_path(app_package: str, patch_filename: str) -> str:
    """Path of app patch, relative to /etc/portage."""
    return os.path.join("patches", app_package, patch_filename)

def read_patch_content(patch_file) -> str:
    file_input_stream = patch_file.read()
    file_size = file_input_stream.query_info("standard::size", None).get_size()
    return file_input_stream.read_bytes(file_size, None).get_data().decode()

def app_portage_files(app_selection) -> dict[str, str]:
    """Return the Portage config and newly selected patch files for an app."""
    app = app_selection.app
    files = {
        portage_config_path(config.directory, app.name): "\n".join(config.entries) + "\n"
        for config in (app_selection.version.config or [])
    }
    files.update({
        portage_patch_path(app.package, patch.get_basename()): read_patch_content(patch)
        for patch in app_selection.patches if isinstance(patch, Gio.File)
    })
    return files
