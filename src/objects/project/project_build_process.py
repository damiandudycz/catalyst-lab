from __future__ import annotations
import json, os, re, threading, shutil, uuid
from datetime import datetime, timezone
from gi.repository import GLib
from .multistage_process import (
    MultiStageProcess, MultiStageProcessStage, MultiStageProcessState, MultiStageProcessStageState,
    MultiStageProcessEvent, MultiStageProcessStageEvent
)
from .root_function import root_function
from .repository import Repository, resolve_path
from .event_bus import SharedEvent
from .emerge_progress import EmergeProgress
from .toolset import BindMount
from .toolset_env_builder import get_stage3_urls
from .project_stage_arguments import StageArgumentDetails
from .project_stage_cache import CACHE_ARGUMENTS, stage_cache_path
from .rootless import (
    prepare_toolset_squashfs, prepare_snapshot_squashfs, unmount_read_only_mount,
    remove_stale_sessions, distfiles_directory, path_is_within
)
from .project_build_rootless import (
    stage_build_session_script, save_interrupted_caches, CATALYST_WRAPPER, catalyst_snapshot_command,
)
from .project_build import StageBuild, StageBuildStatus, StageBuildPlan, project_builds_directory, stage_builds_directory
from .project_build_details import run_details, seed_details, failure_details, output_details, failure_logs_script
from .project_build_spec import (
    StageSpecContext, generate_stage_spec, generate_portage_confdir, generate_root_overlay, seed_name_prefix, select_seed_url, snapshot_treeish
)

# Paths used by catalyst inside toolset.
CATALYST_BUILDS_PATH = "/var/tmp/catalyst/builds"
CATALYST_STAGE_ARTIFACTS_PATH = "/var/tmp/catalystlab-stage-artifacts"
CATALYST_SEEDS_PATH = "/var/tmp/catalystlab-seeds"
CATALYST_KERNEL_CACHE_PATH = "/var/tmp/catalystlab-kerncache"
CATALYST_WORK_PATH = "/var/tmp/catalystlab-work"
CATALYST_SNAPSHOTS_PATH = "/var/tmp/catalyst/snapshots"
# Folders mounted in toolset whose paths contain whitespace are mounted here: catalyst splits spec values on whitespace,
# so their paths can't be used in specs (eg. project "Raspberry Pi 5").
CATALYST_MIRRORS_PATH = "/var/tmp/catalystlab-mirrors"
# Archive extensions catalyst can produce, used to find built stage file.
STAGE_ARCHIVE_EXTENSIONS = (".tar.xz", ".tar.bz2", ".tar.gz", ".tar.zst", ".tar", ".squashfs")

# ------------------------------------------------------------------------------
# Build process.
# ------------------------------------------------------------------------------

class ProjectBuild(MultiStageProcess):
    """Builds stages of project selected in build plan, parents first. Failed stage doesn't stop the process,
    only stages depending on it are skipped, so other branches are still built.
    Builds use snapshot selected for build (project snapshot by default), or latest snapshot generated with project
    toolset before building. Selected snapshot can be stored in project too."""

    def __init__(self, project_directory, plan: StageBuildPlan, snapshot=None, fetch_snapshot: bool = False,
                 update_project_snapshot: bool = False):
        self.project_directory = project_directory
        self.plan = plan
        self._build_order = plan.build_order()
        self.snapshot = None if fetch_snapshot else (snapshot or project_directory.get_snapshot()) # Set when fetched.
        self.fetch_snapshot = fetch_snapshot
        self.update_project_snapshot = update_project_snapshot
        self.toolset = project_directory.get_toolset()
        self.timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") # @TIMESTAMP@ for all stages of this build.
        self.builds_directory = project_builds_directory(project_directory)
        self.catalyst_builds_directory = os.path.join(self.builds_directory, "Catalyst")
        self.stage_artifacts_directory = os.path.join(self.builds_directory, "stages")
        self.kernel_cache_directory = os.path.join(self.builds_directory, "caches", "kerncache")
        self.work_root = os.path.join(resolve_path(Repository.Settings.value.temporary_location), "CatalystBuilds",
                                      project_directory.id.hex, f"{self.timestamp}-{uuid.uuid4().hex}")
        self.seed_subpaths: dict = {} # Downloaded seeds of root stages, relative to builds directory.
        self.stage_builds: dict = {} # Builds made in this run, by stage id.
        self.failed_stage_ids: set = set()
        # Builds run without root privileges in user namespace when system supports it, otherwise in toolset spawned
        # by root helper.
        # Builds of toolsets linked to virtual machine run in that machine (always without root privileges).
        self.machine = self.toolset.machine if self.toolset else None
        self.executor = self.toolset.executor if self.toolset else None
        self._rosetta_amd64: bool | None = None
        self.rootless_unsupported_reason = self.executor.unsupported_reason() if self.executor and not self.machine else None
        self.rootless = self.machine is not None or self.rootless_unsupported_reason is None
        self.rootless_toolset_path: str | None = None # Read-only mounted or extracted toolset lower layer.
        self.rootless_toolset_mounted = False # Mounted SquashFS lower layer, released before machine workspace.
        self.rootless_snapshot_path: str | None = None
        self.rootless_snapshot_mounted = False
        super().__init__(title=f"Building {project_directory.name}")
        # Project rows show running builds in their status indicators, so project state changes with build state.
        self.event_bus.subscribe(MultiStageProcessEvent.STATE_CHANGED, self._on_state_changed)
        # Record of run (steps shown later from builds list) is saved when steps change, also runs interrupted by
        # closing app can be shown then.
        for step in self.stages:
            step.event_bus.subscribe(MultiStageProcessStageEvent.STATE_CHANGED, self._on_step_state_changed)

    def name(self) -> str:
        return self.project_directory.name

    def rosetta_amd64_available(self) -> bool:
        """Whether the running toolset machine has Lima's Rosetta binfmt handler enabled."""
        if self._rosetta_amd64 is None:
            if self.machine is None or self.executor is None:
                self._rosetta_amd64 = False
            else:
                output = self.executor.output(
                    "entry=/proc/sys/fs/binfmt_misc/rosetta; "
                    "[ -r $entry ] && grep -q '^enabled' $entry && echo enabled"
                )
                self._rosetta_amd64 = output == "enabled"
        return self._rosetta_amd64

    def _on_step_state_changed(self, *args):
        if self.status != MultiStageProcessState.SETUP:
            from .project_build_record import save_build_record
            save_build_record(self)

    def _on_state_changed(self, *args):
        self.project_directory.event_bus.emit(SharedEvent.STATE_UPDATED, self.project_directory)

    def setup_stages(self):
        # Latest snapshot is generated in toolset prepared for build, so toolset is prepared once.
        self.stages.append(ProjectBuildStepPrepareToolset(multistage_process=self))
        if self.fetch_snapshot:
            self.stages.append(ProjectBuildStepFetchSnapshot(multistage_process=self))
        for stage in self._build_order:
            if self.plan.parent(stage) is None:
                self.stages.append(ProjectBuildStepDownloadSeed(stage=stage, multistage_process=self))
            self.stages.append(ProjectBuildStepBuildStage(stage=stage, multistage_process=self))

    @property
    def seed_cache_directory(self) -> str:
        """Shared Library cache directory for downloaded source stage archives."""
        return os.path.join(resolve_path(Repository.Settings.value.cache_location), "catalyst-seeds")

    def stage_work_directory(self, stage) -> str:
        """Transient generated files used while building this stage."""
        return os.path.join(self.work_root, stage.id.hex)

    def container_path(self, host_path: str) -> str:
        """Path inside toolset of file in project builds directory, or in mirrored folder (mounted at the same location,
        or under CATALYST_MIRRORS_PATH when its path contains whitespace). Other paths are the same."""
        for root, target in ((self.work_root, CATALYST_WORK_PATH),
                             (self.catalyst_builds_directory, CATALYST_BUILDS_PATH),
                             (self.kernel_cache_directory, CATALYST_KERNEL_CACHE_PATH)):
            if path_is_within(host_path, root):
                relative = os.path.relpath(host_path, root)
                return target if relative == "." else os.path.join(target, relative)
        for root in sorted(self.mirrored_paths(), key=len, reverse=True):
            if path_is_within(host_path, root):
                return self.mirror_path(root) + host_path[len(root):]
        return host_path

    @staticmethod
    def mirror_path(host_path: str) -> str:
        """Location of mirrored folder in toolset: the same path, or path without whitespace."""
        if not any(character.isspace() for character in host_path):
            return host_path
        import hashlib
        return f"{CATALYST_MIRRORS_PATH}/{hashlib.sha1(host_path.encode()).hexdigest()[:16]}"

    def mirrored_paths(self) -> list[str]:
        """Host directories referenced by spec values (@REPO_DIR@, root_overlay, repos, selected cache folders),
        available in toolset at the same paths."""
        project = self.project_directory
        paths = [project.directory_path()]
        if releng_directory := project.get_releng_directory():
            paths.append(releng_directory.directory_path())
        paths += [overlay.directory_path() for overlay in Repository.OverlayDirectory.value]
        paths = [path for path in paths if os.path.isdir(path)]
        # Caches in folders selected by user (automatic ones are in builds directory).
        paths += sorted({
            path for stage in self._build_order for path in self.stage_cache_paths(stage).values()
            if not path_is_within(path, self.builds_directory)
        })
        return paths

    def stage_cache_paths(self, stage) -> dict:
        """Folders of enabled caches of stage, by argument. Automatic caches of machine builds are on disk of machine
        (shared folders can't store owners of files that portage sets), in its cache directory."""
        paths = {
            argument: path for argument in CACHE_ARGUMENTS
            if (path := stage_cache_path(self.project_directory, stage, argument))
        }
        if self.machine:
            paths = {argument: self._machine_cache_path(path) or path for argument, path in paths.items()}
        return paths

    def _saved_cache_roots(self) -> list[tuple[str, str]]:
        """Folders of automatic caches on host, with their folders in cache directory of machine: builds of project
        (kernels), and binary packages folder (shared by projects using it). Ids instead of names, paths in spec can't
        contain spaces."""
        roots = [(self.builds_directory, os.path.join("Projects", self.project_directory.id.hex))]
        if packages_directory := self.project_directory.get_packages_directory():
            roots.append((packages_directory.directory_path(), os.path.join("Packages", packages_directory.id.hex)))
        return roots

    def _machine_cache_path(self, path: str) -> str | None:
        """Folder in machine used for automatic cache saved in path, None for caches selected by user."""
        for root, machine_root in self._saved_cache_roots():
            if path != root and path_is_within(path, root):
                return os.path.join(self.executor.cache_directory(), machine_root, os.path.relpath(path, root))
        return None

    def synced_caches(self, stage) -> list[tuple[str, str]]:
        """Caches of machine build kept in shared folders between builds, as (saved path, path in working space).
        Working space is deleted after build, caches are saved as plain files (shared folders can't store owners).
        Kernel cache isn't saved: it contains kernel sources, which have files with names differing only in case
        (eg. xt_CONNMARK.h and xt_connmark.h), and shared folders of macOS are case-insensitive, so saved sources were
        broken and next builds failed using them. It's used only by stages of the same build then (in working space).
        Saving it wouldn't make builds faster anyway: genkernel cleans sources (make mrproper) and compiles kernel
        again, only unpacking of sources is skipped."""
        if not self.machine:
            return []
        caches = [(distfiles_directory(), distfiles_directory(self.executor))]
        for argument in CACHE_ARGUMENTS:
            if argument == StageArgumentDetails.kerncache_path:
                continue
            saved = stage_cache_path(self.project_directory, stage, argument)
            if saved and (machine_path := self._machine_cache_path(saved)):
                caches.append((saved, machine_path))
        return caches

    def make_directories(self, paths: list[str]):
        """Creates directories, ones in cache or temporary directory of machine are created in machine."""
        machine_paths = [path for path in paths if self.machine and self.executor.owns_path(path)]
        for path in paths:
            if path not in machine_paths:
                os.makedirs(path, exist_ok=True)
        if machine_paths:
            self.executor.filesystem({"mkdir": machine_paths})

    def seed_subpath(self, stage) -> str | None:
        """Seed of stage, relative to builds directory, without extension (as catalyst expects source_subpath)."""
        parent = self.plan.parent(stage)
        if parent is None:
            return self.seed_subpaths.get(stage.id)
        build = self.stage_builds.get(parent.id) or self.plan.entries[parent.id].reused_build
        if build is None or not build.artifact_path:
            return None
        return _strip_archive_extension(os.path.join(
            "..", "..", os.path.basename(CATALYST_STAGE_ARTIFACTS_PATH),
            os.path.relpath(build.artifact_path, self.stage_artifacts_directory),
        ))

    def is_blocked(self, step: MultiStageProcessStage) -> str | None:
        """Reason why step can't run because something it depends on failed."""
        if isinstance(self.stages[0], ProjectBuildStepPrepareToolset) and self.stages[0].state == MultiStageProcessStageState.FAILED:
            return "Toolset preparation failed"
        stage = getattr(step, "stage", None)
        while stage is not None:
            if stage.id in self.failed_stage_ids:
                return f"{stage.name} failed" if stage is not step.stage else "Seed download failed"
            stage = self.plan.parent(stage)
        return None

    def _continue_process(self):
        """Like base implementation, but failed steps only skip steps that depend on them."""
        if self.status in (MultiStageProcessState.COMPLETED, MultiStageProcessState.FAILED, MultiStageProcessState.SETUP):
            return
        for step in self.stages:
            if step.state == MultiStageProcessStageState.FAILED and (stage := getattr(step, "stage", None)):
                self.failed_stage_ids.add(stage.id)
        for step in self.stages:
            if step.state != MultiStageProcessStageState.SCHEDULED:
                continue
            if reason := self.is_blocked(step):
                step.log(f"Skipped: {reason}")
                step._update_state(MultiStageProcessStageState.FAILED)
                if stage := getattr(step, "stage", None):
                    self.failed_stage_ids.add(stage.id)
                continue
            threading.Thread(target=step.start).start()
            return
        # All steps finished:
        success = not any(step.state == MultiStageProcessStageState.FAILED for step in self.stages)
        self.status = MultiStageProcessState.COMPLETED if success else MultiStageProcessState.FAILED
        self.event_bus.emit(MultiStageProcessEvent.STATE_CHANGED, self.status)
        try:
            self._cleanup()
        except Exception as e:
            print(e)
        finally:
            if success:
                MultiStageProcess.started_processes.remove(self)
            # Failed builds stay in started processes (to see their output), but views showing running builds need
            # to be refreshed in both cases.
            MultiStageProcess.event_bus.emit(
                MultiStageProcessEvent.STARTED_PROCESSES_CHANGED,
                self.__class__,
                MultiStageProcess.get_started_processes_by_class(self.__class__)
            )
            self.complete_process(success=success)

    # Records of stages in this run are saved when it starts, so stages that don't start (skipped after failure of
    # stage they depend on, or canceled) are listed in builds too.

    def start(self, authorization_keeper=None):
        for order, stage in enumerate(self._build_order):
            try:
                StageBuild(stage_id=stage.id, stage_name=stage.name, timestamp=self.timestamp, order=order,
                           status=StageBuildStatus.SCHEDULED).save(self.project_directory)
            except OSError as e:
                print(f"Failed to save scheduled build of {stage.name}: {e}")
        super().start(authorization_keeper)

    def cancel(self):
        self.canceled = True
        super().cancel()

    def store_project_snapshot(self):
        """Selected snapshot becomes snapshot of project."""
        if self.snapshot is None:
            return False
        metadata = self.project_directory.initialize_metadata()
        metadata.snapshot_id = self.snapshot.filename
        metadata.latest_snapshot = False
        Repository.ProjectDirectory.save()
        return False

    def complete_process(self, success: bool):
        status = StageBuildStatus.CANCELED if getattr(self, "canceled", False) else StageBuildStatus.SKIPPED
        for stage in self._build_order:
            path = os.path.join(stage_builds_directory(self.project_directory, stage.id), self.timestamp)
            try:
                with open(os.path.join(path, StageBuild.METADATA_FILE), encoding="utf-8") as file:
                    build = StageBuild.init_from(json.load(file), path=path)
                if build.status == StageBuildStatus.SCHEDULED:
                    build.status = status
                    build.save(self.project_directory)
            except (OSError, ValueError, KeyError) as e:
                print(f"Failed to update build of {stage.name}: {e}")
        # Steps of run can be shown later from builds list.
        from .project_build_record import save_build_record
        save_build_record(self)

def start_project_build(project_directory, plan: StageBuildPlan, snapshot=None, fetch_snapshot: bool = False,
                        update_project_snapshot: bool = False, done=None):
    """Starts build of project (used by build view and build queue). Builds that can't run in user namespace get root
    privileges first (user is asked). Done is called on main thread with started build, or None when it didn't start."""
    from .rootless import rootless_unsupported_reason
    from .root_helper_client import RootHelperClient
    def start(authorization_keeper):
        build = None
        try:
            build = ProjectBuild(project_directory=project_directory, plan=plan, snapshot=snapshot,
                                 fetch_snapshot=fetch_snapshot, update_project_snapshot=update_project_snapshot)
            build.start(authorization_keeper=authorization_keeper)
        except Exception as e:
            print(f"Failed to start build of {project_directory.name}: {e}")
        finally:
            if authorization_keeper:
                authorization_keeper.release()
        if build is not None and build.status == MultiStageProcessState.SETUP:
            build = None
        if done:
            done(build)
        return False
    toolset = project_directory.get_toolset()
    if (toolset and toolset.machine) or rootless_unsupported_reason() is None:
        # Builds run in user namespace, root privileges are not needed.
        GLib.idle_add(start, None)
        return
    def authorized(authorization_keeper):
        # Called from background thread. Keeper is released when this callback returns, retain it until build
        # is started on main thread and retains it by itself.
        if authorization_keeper:
            authorization_keeper.retain()
            GLib.idle_add(start, authorization_keeper)
        elif done:
            GLib.idle_add(lambda: (done(None), False)[1]) # Authorization canceled.
    RootHelperClient.shared().authorize_and_run(name="Build stages", callback=authorized)

def running_project_build(project_directory, timestamp: str | None = None) -> ProjectBuild | None:
    """Build of project that is currently in progress, optionally only build started with given timestamp."""
    return next((
        build for build in MultiStageProcess.get_started_processes_by_class(ProjectBuild)
        if build.project_directory.id == project_directory.id
        and build.status == MultiStageProcessState.IN_PROGRESS
        and (timestamp is None or build.timestamp == timestamp)
    ), None)

# ------------------------------------------------------------------------------
# Build steps.
# ------------------------------------------------------------------------------

class ProjectBuildStep(MultiStageProcessStage):
    def start(self):
        self.namespace_processes = [] # Rootless processes, terminated when canceled.
        super().start()
    def cancel(self):
        super().cancel()
        for process in getattr(self, "namespace_processes", []):
            process.terminate()
    def run_command_in_toolset(self, command: str) -> bool:
        """Runs command in toolset, its output is added to step output."""
        self.log(f"$ {command}")
        return super().run_command_in_toolset(command)

class ProjectBuildStepFetchSnapshot(ProjectBuildStep):
    """Generates latest snapshot of Gentoo repository with catalyst -s (like Snapshots section does), in toolset
    prepared for build, and uses it in this build. Snapshot is stored with other snapshots, and in project when user
    chose that."""
    def __init__(self, multistage_process: ProjectBuild):
        super().__init__(name="Get latest snapshot", description="Generates snapshot of Gentoo repository with project toolset", multistage_process=multistage_process)
    def start(self):
        super().start()
        try:
            from .snapshot import existing_latest_snapshot
            process = self.multistage_process
            # Snapshot of current commit of Gentoo repository is used when it exists, without downloading it again.
            snapshot = existing_latest_snapshot(self.log) or self._generate_snapshot()
            process.snapshot = snapshot
            self.log(f"Using snapshot {snapshot.filename} ({snapshot.name})")
            if process.rootless:
                (process.rootless_snapshot_path,
                 process.rootless_snapshot_mounted) = prepare_snapshot_squashfs(
                    snapshot.file_path(), self.log, self.namespace_processes, executor=process.executor)
            if process.update_project_snapshot:
                GLib.idle_add(process.store_project_snapshot)
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during snapshot generation: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

    def _generate_snapshot(self):
        from .snapshot import Snapshot
        from .snapshot_manager import SnapshotManager
        from .snapshot_installation import delete_file, unlock_file_access
        process = self.multistage_process
        snapshots = resolve_path(Repository.Settings.value.snapshots_location)
        os.makedirs(snapshots, exist_ok=True)
        started = datetime.now().timestamp() - 1
        command = catalyst_snapshot_command()
        if process.rootless:
            # Same session as stage builds, catalyst writes snapshot to snapshots folder.
            self.log(f"$ {command}")
            script = stage_build_session_script(executor=process.executor, toolset_path=process.rootless_toolset_path,
                                                bindings=[(snapshots, CATALYST_SNAPSHOTS_PATH)], command=command)
            success = process.executor.run_in_namespace(script, self.log, self.namespace_processes)
        else:
            success = self.run_command_in_toolset(command) # Toolset is spawned with snapshots folder bound.
        if not success:
            raise RuntimeError(f"{command} failed")
        # Snapshot with the same name is replaced when repository didn't change since it was generated.
        written = [name for name in os.listdir(snapshots)
                   if name.endswith(".sqfs") and os.path.getmtime(os.path.join(snapshots, name)) >= started]
        if not written:
            raise RuntimeError("Generated snapshot was not found")
        filename = max(written, key=lambda name: os.path.getmtime(os.path.join(snapshots, name)))
        path = os.path.join(snapshots, filename)
        delete_file(file_path=os.path.join(snapshots, os.path.splitext(filename)[0] + ".lock"), root_dir=snapshots)
        unlock_file_access(path=path)
        snapshot = Snapshot(filename=filename, date=_snapshot_timestamp(path) or datetime.now())
        GLib.idle_add(SnapshotManager.shared().add_snapshot, snapshot)
        return snapshot

def _snapshot_timestamp(path: str):
    """Date of Gentoo repository in snapshot, None when it can't be read."""
    import subprocess
    from .helper_functions import parse_strict_rfc_datetime
    try:
        output = subprocess.check_output(["unsquashfs", "-cat", path, "metadata/timestamp.chk"], text=True)
        return parse_strict_rfc_datetime(output.strip())
    except Exception as e:
        print(f"Failed to read snapshot timestamp: {e}")
        return None

class ProjectBuildStepPrepareToolset(ProjectBuildStep):
    def __init__(self, multistage_process: ProjectBuild):
        super().__init__(name="Prepare toolset", description="Spawns toolset with builds, snapshots and project directories", multistage_process=multistage_process)
        self.reserved = False
        self.spawned = False
    def start(self):
        super().start()
        try:
            toolset = self.multistage_process.toolset
            if toolset is None:
                raise RuntimeError("Project has no toolset")
            if reason := toolset.busy_reason:
                raise RuntimeError(reason)
            if not toolset.reserve():
                raise RuntimeError(f"Toolset {toolset.name} is used by another operation")
            self.reserved = True
            process = self.multistage_process
            for directory in (process.builds_directory, process.catalyst_builds_directory,
                              process.stage_artifacts_directory, process.kernel_cache_directory):
                os.makedirs(directory, exist_ok=True)
            if self.multistage_process.rootless:
                self._prepare_rootless()
                self.complete(MultiStageProcessStageState.COMPLETED)
                return
            self.log(f"Building with root privileges: {self.multistage_process.rootless_unsupported_reason}")
            # Catalyst mounts snapshot squashfs with loop device. Loop device nodes are created by kernel in host /dev,
            # so free ones are prepared before spawning and only they are made available in toolset.
            self.loop_devices = prepare_loop_devices()
            for binding in self.required_bindings():
                self.log(f"Binding {binding.host_path} -> {binding.mount_path}")
            toolset.spawn(additional_bindings=self.required_bindings())
            if not toolset.spawned:
                raise RuntimeError("Failed to spawn toolset")
            self.spawned = True
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during '{self.name}': {e}")
            self.complete(MultiStageProcessStageState.FAILED)
    def _prepare_rootless(self):
        """Prepares toolset and snapshot lower layers used by stage builds."""
        process = self.multistage_process
        executor = process.executor
        if process.machine:
            self.log(f"Building in virtual machine {process.machine.name}, without root privileges")
            process.machine.ensure_running(self.log, self.namespace_processes)
            self.workspace_user = f"Build of {process.project_directory.name}"
            process.machine.acquire_workspace(self.log, self.namespace_processes, user=self.workspace_user)
            self.workspace_acquired = True
        else:
            self.log("Building without root privileges, in user namespace")
        remove_stale_sessions(self.log, self.namespace_processes, executor=executor)
        process.rootless_toolset_path, process.rootless_toolset_mounted = prepare_toolset_squashfs(
            process.toolset.file_path(), self.log, self.namespace_processes, executor=executor)
        # Latest snapshot is extracted after it's generated.
        if not process.fetch_snapshot:
            snapshot = process.snapshot
            if snapshot is None:
                raise RuntimeError("Build has no snapshot")
            (process.rootless_snapshot_path,
             process.rootless_snapshot_mounted) = prepare_snapshot_squashfs(
                snapshot.file_path(), self.log, self.namespace_processes, executor=executor)
        process.make_directories([distfiles_directory(executor), process.seed_cache_directory, process.work_root] + process.mirrored_paths())
    def required_bindings(self) -> list[BindMount]:
        process = self.multistage_process
        bindings = [
            # Catalyst keeps rel_type output inside this dedicated folder, separate from app-owned build data.
            BindMount(mount_path=CATALYST_BUILDS_PATH, host_path=process.catalyst_builds_directory, store_changes=True, create_if_missing=True),
            # Stage inputs and downloaded seeds are mounted alongside Catalyst's builds directory. Their source_subpath
            # values use ../ so arbitrary rel_type names cannot collide with these input mounts.
            BindMount(mount_path=CATALYST_STAGE_ARTIFACTS_PATH, host_path=process.stage_artifacts_directory, store_changes=True, create_if_missing=True),
            BindMount(mount_path=CATALYST_SEEDS_PATH, host_path=process.seed_cache_directory, store_changes=True, create_if_missing=True),
            BindMount(mount_path=CATALYST_KERNEL_CACHE_PATH, host_path=process.kernel_cache_directory, store_changes=True, create_if_missing=True),
            # Generated specs and scripts for this build run only.
            BindMount(mount_path=CATALYST_WORK_PATH, host_path=process.work_root, store_changes=True, create_if_missing=True),
            BindMount(mount_path=CATALYST_SNAPSHOTS_PATH,
                      host_path=resolve_path(Repository.Settings.value.snapshots_location), store_changes=True),
        ]
        # Directories referenced by spec values with host paths (@REPO_DIR@, root_overlay, repos, cache folders) are
        # available at the same paths.
        bindings += [BindMount(mount_path=process.mirror_path(path), host_path=path, store_changes=True, create_if_missing=True) for path in process.mirrored_paths()]
        # Loop devices for mounting snapshot (store_changes gives read-write device access).
        bindings += [BindMount(mount_path=path, host_path=path, store_changes=True) for path in getattr(self, "loop_devices", [])]
        return bindings
    def cleanup(self) -> bool:
        if not super().cleanup():
            return False
        process = self.multistage_process
        toolset = process.toolset
        if self.spawned:
            toolset.unspawn(rebuild_squashfs_if_needed=False)
        if getattr(self, "workspace_acquired", False):
            # Working space is deleted with caches changed by canceled build, they are saved first.
            try:
                save_interrupted_caches(process.executor, self.log)
            except Exception as e:
                self.log(f"Failed to save caches: {e}")
            for path_attribute, mounted_attribute, description in (
                ("rootless_toolset_path", "rootless_toolset_mounted", "toolset image"),
                ("rootless_snapshot_path", "rootless_snapshot_mounted", "Gentoo snapshot"),
            ):
                if not getattr(process, mounted_attribute):
                    continue
                try:
                    unmount_read_only_mount(getattr(process, path_attribute), self.log, executor=process.executor)
                    setattr(process, mounted_attribute, False)
                    setattr(process, path_attribute, None)
                except Exception as e:
                    # Releasing the workspace recursively unmounts any remaining mount as a fallback.
                    self.log(f"Failed to unmount {description}: {e}")
            process.machine.release_workspace(self.log, user=self.workspace_user)
        shutil.rmtree(process.work_root, ignore_errors=True)
        if self.reserved:
            toolset.release()
        return True

class ProjectBuildStepDownloadSeed(ProjectBuildStep):
    def __init__(self, stage, multistage_process: ProjectBuild):
        super().__init__(name=f"Download seed for {stage.name}", description="Downloads latest stage3 from Gentoo mirrors", multistage_process=multistage_process)
        self.stage = stage
    def start(self):
        super().start()
        try:
            import requests
            process = self.multistage_process
            architecture = process.project_directory.get_architecture()
            prefix = seed_name_prefix(process.project_directory, self.stage)
            self.log(f"Looking for latest {prefix}")
            result = get_stage3_urls(architecture)
            if isinstance(result, Exception):
                raise RuntimeError(f"Failed to read list of stage3 files: {result}")
            url = select_seed_url(result, prefix)
            if url is None:
                raise RuntimeError(f"No {prefix} found in latest stage3 files")
            filename = os.path.basename(url.path)
            seeds_directory = process.seed_cache_directory
            path = os.path.join(seeds_directory, filename)
            previous_path = os.path.join(process.builds_directory, "seeds", filename)
            if os.path.isfile(path):
                self.log(f"Using already downloaded {filename}")
            elif os.path.isfile(previous_path):
                os.makedirs(seeds_directory, exist_ok=True)
                os.replace(previous_path, path)
                self.log(f"Reused cached seed {filename}")
            else:
                os.makedirs(seeds_directory, exist_ok=True)
                self.log(f"Downloading {url.geturl()}")
                partial_path = f"{path}.{uuid.uuid4().hex}.part"
                try:
                    with requests.get(url.geturl(), stream=True, timeout=60) as response:
                        response.raise_for_status()
                        total = int(response.headers.get("content-length", 0))
                        downloaded = 0
                        with open(partial_path, "wb") as file:
                            for chunk in response.iter_content(chunk_size=1024 * 1024):
                                if self._cancel_event.is_set():
                                    raise RuntimeError("Canceled")
                                file.write(chunk)
                                downloaded += len(chunk)
                                if total:
                                    self._update_progress(downloaded / total)
                    os.replace(partial_path, path)
                finally:
                    if os.path.exists(partial_path):
                        os.remove(partial_path)
            process.seed_subpaths[self.stage.id] = os.path.join(
                "..", "..", os.path.basename(CATALYST_SEEDS_PATH), _strip_archive_extension(filename)
            )
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during '{self.name}': {e}")
            self.complete(MultiStageProcessStageState.FAILED)

class ProjectBuildStepBuildStage(ProjectBuildStep):
    def __init__(self, stage, multistage_process: ProjectBuild):
        super().__init__(name=f"Build {stage.name}", description=f"Builds {stage.target.replace('_', '-')} with catalyst", multistage_process=multistage_process)
        self.stage = stage
        self.build: StageBuild | None = None
        self._log_file = None
        self._emerge_progress = EmergeProgress(completed_only=True, ignore_seed_update=True)
    def log(self, line: str):
        """Output is also saved as build.log in build directory, to keep it after app is closed. Packages built by
        catalyst are shown as progress."""
        super().log(line)
        if (progress := self._emerge_progress.parse(line)) is not None:
            self._update_progress(progress)
        if self.build and self.build.path:
            try:
                if self._log_file is None:
                    self._log_file = open(os.path.join(self.build.path, "build.log"), "a", encoding="utf-8", buffering=1)
                self._log_file.write(line + "\n")
            except OSError as e:
                print(f"Failed to write build log: {e}")
    def complete(self, state: MultiStageProcessStageState):
        super().complete(state) # Can log reason of failure.
        if self._log_file:
            self._log_file.close()
            self._log_file = None
    def start(self):
        super().start()
        process = self.multistage_process
        project = process.project_directory
        try:
            self.build = StageBuild(stage_id=self.stage.id, stage_name=self.stage.name, timestamp=process.timestamp,
                                    order=process._build_order.index(self.stage))
            self.build.details = run_details(process)
            self.build.details["inputs"]["seed"] = seed_details(process, self.stage)
            self.build.save(project)
            # Seed:
            source_subpath = process.seed_subpath(self.stage)
            if source_subpath is None:
                raise RuntimeError("Seed of stage is not available")
            # Portage configuration and spec:
            work_directory = process.stage_work_directory(self.stage)
            if os.path.isdir(work_directory):
                shutil.rmtree(work_directory)
            os.makedirs(work_directory)
            portage_path = os.path.join(work_directory, "portage")
            has_confdir = generate_portage_confdir(project, self.stage, portage_path)
            root_overlay_path = os.path.join(work_directory, "root_overlay")
            has_root_overlay = generate_root_overlay(project, self.stage, root_overlay_path)
            iso_overlay_path = os.path.join(work_directory, "iso_overlay")
            has_iso_overlay = generate_root_overlay(project, self.stage, iso_overlay_path, StageArgumentDetails.livecd_overlay)
            cache_paths = process.stage_cache_paths(self.stage)
            process.make_directories(list(cache_paths.values()))
            rosetta_amd64 = process.rosetta_amd64_available()
            spec = generate_stage_spec(project, self.stage, StageSpecContext(
                timestamp=process.timestamp,
                source_subpath=source_subpath,
                portage_confdir=process.container_path(portage_path) if has_confdir else None,
                root_overlay=process.container_path(root_overlay_path) if has_root_overlay else None,
                iso_overlay=process.container_path(iso_overlay_path) if has_iso_overlay else None,
                cache_paths={argument: process.container_path(path) for argument, path in cache_paths.items()},
                snapshot=process.snapshot,
                path_mapper=process.container_path,
                rosetta_amd64=rosetta_amd64,
            ))
            if rosetta_amd64:
                self.log("Rosetta is active for amd64; Catalyst interpreter is omitted")
            values = _spec_values(spec)
            spec_path = os.path.join(work_directory, "stage.spec")
            with open(spec_path, "w", encoding="utf-8") as file:
                file.write(spec)
            # Spec and portage configuration are kept with build (work directory is temporary).
            shutil.copyfile(spec_path, os.path.join(self.build.path, StageBuild.SPEC_FILE))
            self.build.details["inputs"]["spec"] = StageBuild.SPEC_FILE
            from .build_failures import stage_configuration_key
            toolset_details = self.build.details["inputs"].get("toolset") or {}
            if has_confdir:
                shutil.copytree(portage_path, os.path.join(self.build.path, StageBuild.PORTAGE_DIRECTORY), symlinks=True, dirs_exist_ok=True)
                self.build.details["inputs"]["portage"] = StageBuild.PORTAGE_DIRECTORY
            self.build.details["inputs"]["configuration"] = stage_configuration_key(
                spec, process.timestamp, toolset_details.get("catalyst"))
            self.build.save(project)
            self.log("Spec:")
            for line in spec.splitlines():
                self.log(f"  {line}")
            # Build:
            wrapper_path = None
            if process.rootless:
                wrapper_path = os.path.join(work_directory, "catalyst-wrapper.py")
                with open(wrapper_path, "w", encoding="utf-8") as file:
                    file.write(CATALYST_WRAPPER)
            build_script_path = os.path.join(work_directory, "build.sh")
            with open(build_script_path, "w", encoding="utf-8") as file:
                file.write(_catalyst_build_script(
                    spec_path=process.container_path(spec_path),
                    config_path=process.container_path(os.path.join(work_directory, "catalyst.conf")),
                    caches={_catalyst_cache_options[argument] for argument in cache_paths},
                    wrapper_path=process.container_path(wrapper_path) if wrapper_path else None,
                ))
            if process.rootless:
                if not self._run_rootless(spec=spec, work_directory=work_directory, build_script_path=build_script_path):
                    raise RuntimeError("Catalyst build failed")
            # Command is passed in single quotes, path in double quotes allows spaces in stage name.
            elif not self.run_command_in_toolset(f'bash "{process.container_path(build_script_path)}"'):
                self._run_diagnostics(spec=spec, work_directory=work_directory)
                raise RuntimeError("Catalyst build failed")
            # Move results to stage build directory (catalyst writes them to builds/<rel_type>/):
            output_name = f"{values['target']}-{values['subarch']}-{values['version_stamp']}"
            output_directory = os.path.abspath(os.path.join(process.catalyst_builds_directory, values["rel_type"]))
            if os.path.commonpath((process.catalyst_builds_directory, output_directory)) != process.catalyst_builds_directory:
                raise RuntimeError(f"Catalyst rel_type escapes its output directory: {values['rel_type']}")
            # ISO of livecd-stage2 is written to path from livecd/iso (relative paths are in builds/<rel_type>/ too).
            iso = values.get("livecd/iso") or None
            iso_name = iso if iso and "/" not in iso else None
            if process.rootless:
                # Files written by namespace root are already owned by user.
                moved = _move_stage_build_files(source_directory=output_directory, name=output_name, destination_directory=self.build.path, iso_name=iso_name)
            else:
                moved = move_stage_build_files(source_directory=output_directory, name=output_name, destination_directory=self.build.path, iso_name=iso_name)
            artifact = next((filename for filename in moved if filename.endswith(STAGE_ARCHIVE_EXTENSIONS)), None)
            if artifact is None:
                raise RuntimeError(f"Built stage {output_name} not found in {output_directory}")
            try:
                os.rmdir(output_directory)
            except OSError:
                pass # Keep the folder if Catalyst or another process left any files there.
            self.log(f"Saved {', '.join(moved)} to {self.build.path}")
            self.build.artifact = artifact
            self.build.status = StageBuildStatus.COMPLETED
            self.build.finished = datetime.now()
            self.build.details["output"] = output_details(self.build)
            if iso_file := next((filename for filename in moved if filename.endswith(".iso")), None):
                # Livecd: ISO is result of build. Archive captured by catalyst contains the same files (root of ISO),
                # it's removed after packages of stage are read from its CONTENTS.
                for filename in moved:
                    if filename == artifact or filename.startswith(artifact + "."):
                        try:
                            os.remove(os.path.join(self.build.path, filename))
                        except OSError as e:
                            self.log(f"Failed to remove {filename}: {e}")
                self.log(f"Removed {artifact} (contains the same files as {iso_file})")
                self.build.artifact = iso_file
                self.build.details["output"]["size"] = os.path.getsize(os.path.join(self.build.path, iso_file))
            self.build.save(project)
            from .build_failures import reconcile_project_failure_states
            reconcile_project_failure_states(project)
            process.stage_builds[self.stage.id] = self.build
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during '{self.name}': {e}")
            # Canceling build run terminates catalyst, which isn't failure of stage.
            stopped = getattr(process, "canceled", False)
            if self.build:
                self.build.status = StageBuildStatus.STOPPED if stopped else StageBuildStatus.FAILED
                self.build.finished = datetime.now()
                try:
                    if not stopped:
                        failure_directory = os.path.join(self.build.path, StageBuild.FAILURE_DIRECTORY) if self.build.path else None
                        self.build.details["failure"] = failure_details(self.output_lines + [str(e)], failure_directory)
                except Exception as details_error:
                    print(f"Failed to read failure details: {details_error}")
                try:
                    self.build.save(project)
                    if not stopped:
                        from .build_failures import reconcile_project_failure_states
                        reconcile_project_failure_states(project)
                except Exception as save_error:
                    print(f"Failed to save build status: {save_error}")
            self.complete(MultiStageProcessStageState.FAILED)

    def _run_rootless(self, spec: str, work_directory: str, build_script_path: str) -> bool:
        """Runs build script in toolset root inside user namespace, with diagnostics after failure."""
        process = self.multistage_process
        diagnostics_path = self._write_diagnostics_script(spec=spec, work_directory=work_directory, rootless=True)
        bindings = [
            (process.catalyst_builds_directory, CATALYST_BUILDS_PATH),
            (process.stage_artifacts_directory, CATALYST_STAGE_ARTIFACTS_PATH),
            (process.seed_cache_directory, CATALYST_SEEDS_PATH),
            (process.kernel_cache_directory, CATALYST_KERNEL_CACHE_PATH),
            (process.work_root, CATALYST_WORK_PATH),
            (process.rootless_snapshot_path, f"{CATALYST_SNAPSHOTS_PATH}/gentoo-{snapshot_treeish(process.project_directory, process.snapshot)}.sqfs"),
            (distfiles_directory(process.executor), "/var/cache/distfiles"),
        ] + [(path, process.mirror_path(path)) for path in process.mirrored_paths()]
        script = stage_build_session_script(
            executor=process.executor,
            toolset_path=process.rootless_toolset_path,
            bindings=bindings,
            command=f'bash "{process.container_path(build_script_path)}"',
            diagnostics_command=f'bash "{process.container_path(diagnostics_path)}"' if diagnostics_path else None,
            synced_caches=process.synced_caches(self.stage),
            patch_catalyst_stage1=True,
        )
        self.log(f"$ bash {process.container_path(build_script_path)}")
        return process.executor.run_in_namespace(script, self.log, self.namespace_processes)

    def _run_diagnostics(self, spec: str, work_directory: str):
        """Collects details about chroot left by failed catalyst build. Catalyst hides errors of some scripts
        (eg. stage1 build.py), this runs the same checks with errors visible."""
        if script_path := self._write_diagnostics_script(spec=spec, work_directory=work_directory, rootless=False):
            self.run_command_in_toolset(f'bash "{self.multistage_process.container_path(script_path)}"')

    def _write_diagnostics_script(self, spec: str, work_directory: str, rootless: bool) -> str | None:
        """Writes diagnostics script, returns its path. Rootless builds use extracted snapshot folder instead of
        squashfs file, which is bound instead of loop mounted."""
        try:
            process = self.multistage_process
            values = _spec_values(spec)
            chroot = f"/var/tmp/catalyst/tmp/{values['rel_type']}/{values['target']}-{values['subarch']}-{values['version_stamp']}"
            snapshot = f"{CATALYST_SNAPSHOTS_PATH}/gentoo-{values['snapshot_treeish']}.sqfs"
            profile = values.get("profile", "")
            # Logs for bug reports are saved in build directory (available in toolset at path of builds directory).
            failure_logs = failure_logs_script(chroot, process.container_path(os.path.join(self.build.path, StageBuild.FAILURE_DIRECTORY))) if self.build and self.build.path else ""
            script = f"""#!/bin/bash
CHROOT="{chroot}"
REPO="$CHROOT/var/db/repos/gentoo"
echo "===== Diagnostics of failed build ====="
echo "--- chroot: $CHROOT"; ls "$CHROOT" | head -30
echo "--- make.profile:"; ls -l "$CHROOT/etc/portage/make.profile"
echo "--- make.conf:"; cat "$CHROOT/etc/portage/make.conf"
echo "--- /etc/portage:"; find "$CHROOT/etc/portage" -maxdepth 3 | head -60
echo "--- repos.conf:"; cat "$CHROOT"/etc/portage/repos.conf/* 2>&1 | head -20
echo "--- mounts in chroot:"; grep "$CHROOT" /proc/mounts
mkdir -p "$REPO"
echo "--- mounting snapshot {snapshot}:"; mount {"--bind" if rootless else "-o ro,loop"} "{snapshot}" "$REPO" && echo "mounted"
mount --bind /proc "$CHROOT/proc"
{failure_logs}
echo "--- repo profiles:"; ls "$REPO/profiles" | head -20
echo "--- profile {profile}:"; ls "$REPO/profiles/{profile}"
echo "--- portage in chroot:"
chroot "$CHROOT" /bin/bash -c 'for module in /usr/lib/python3*/site-packages/portage/__init__.py; do interpreter=$(echo $module | cut -d/ -f4); echo "using $interpreter"; $interpreter -c "import portage; print(portage.settings.profiles)"; $interpreter /tmp/build.py; echo "build.py exit code: $?"; done'
umount "$CHROOT/proc"
umount "$REPO"
echo "===== End of diagnostics ====="
"""
            script_path = os.path.join(work_directory, "diagnostics.sh")
            with open(script_path, "w", encoding="utf-8") as file:
                file.write(script)
            return script_path
        except Exception as e:
            self.log(f"Failed to prepare diagnostics: {e}")
            return None

# ------------------------------------------------------------------------------
# Helper functions.
# ------------------------------------------------------------------------------

# Catalyst options enabling caches.
_catalyst_cache_options = {
    StageArgumentDetails.pkgcache_path: "pkgcache",
    StageArgumentDetails.kerncache_path: "kerncache",
}

def _catalyst_build_script(
    spec_path: str,
    config_path: str,
    caches: set[str],
    wrapper_path: str | None = None,
) -> str:
    """Runs catalyst with /dev containing real device nodes. Catalyst bind mounts /dev into chroot without submounts,
    but device nodes in toolset /dev are bind mounts made by bwrap, so chroot would get empty files instead (portage
    fails with '/dev/null is not a character device'). Only standard nodes, loop devices and kvm are created.
    Catalyst uses configuration of toolset, with cache options enabled only for caches used by stage, and parallel
    builds using all processors when toolset doesn't configure them. Without jobs, catalyst sets empty MAKEOPTS,
    which disables default of portage (-j<processors>), so packages would be built one by one with single process.
    In rootless builds (wrapper_path set) /dev is prepared by session script, and catalyst runs through wrapper."""
    enabled = " ".join(sorted(caches))
    disabled = " ".join(sorted(set(_catalyst_cache_options.values()) - caches))
    if wrapper_path:
        run = f'exec python3 "{wrapper_path}" "${{CONFIG_ARGS[@]}}" -f "{spec_path}"'
    else:
        run = _CATALYST_DEV_SETUP + f'exec catalyst "${{CONFIG_ARGS[@]}}" -f "{spec_path}"'
    return f"""#!/bin/bash
# Use QEMU's broadest CPU model for user-mode binfmt emulation. Keep this in the
# build process environment: binfmt_misc cannot reliably use a shell script as
# its interpreter, and this does not alter the generated stage or Portage config.
export QEMU_CPU=max
# Fix of older catalyst: stage1 build.py concatenates portage Atom with str (TypeError with current portage). Fixed
# upstream, applied to toolset copy of this build session only (changes are not stored in toolset).
for build_py in /usr/share/catalyst/targets/stage1/build.py; do
    [ -f "$build_py" ] && grep -q 'sys.stdout.write(b + " ")' "$build_py" \
        && sed -i 's/sys.stdout.write(b + " ")/sys.stdout.write(str(b) + " ")/' "$build_py" \
        && echo "Applied stage1 build.py fix to catalyst"
done
# Catalyst configuration with cache options matching stage settings. Configuration of older catalyst (not TOML) is
# used unchanged.
CONFIG_ARGS=()
if python3 - "{config_path}" "{enabled}" "{disabled}" <<'PYTHON'
import json, os, sys, tomllib
path, enabled, disabled = sys.argv[1], sys.argv[2].split(), sys.argv[3].split()
with open("/etc/catalyst/catalyst.conf", "rb") as file:
    config = tomllib.load(file)
if any(isinstance(value, dict) for value in config.values()):
    sys.exit("Unsupported catalyst.conf structure")
options = [option for option in config.get("options", []) if option not in disabled]
config["options"] = options + [option for option in enabled if option not in options]
# emerge --jobs and --load-average, and MAKEOPTS (-j, -l). Load average limits parallel jobs to processors count.
processors = os.cpu_count() or 1
config.setdefault("jobs", processors)
config.setdefault("load-average", float(processors))
with open(path, "w") as file:
    for key, value in config.items():
        file.write(f"{{key}} = {{json.dumps(value)}}\\n")
print("Catalyst options: " + ", ".join(config["options"]))
print(f"Parallel jobs: {{config['jobs']}}, load average: {{config['load-average']}}")
PYTHON
then
    CONFIG_ARGS=(-c "{config_path}")
else
    echo "Using toolset catalyst.conf without changes"
fi
{run}
"""

_CATALYST_DEV_SETUP = """set -e
DEV=/tmp/catalystlab-dev
mkdir -p "$DEV"
mount -t tmpfs -o mode=0755,nosuid catalystlab-dev "$DEV"
mknod -m 666 "$DEV/null" c 1 3
mknod -m 666 "$DEV/zero" c 1 5
mknod -m 666 "$DEV/full" c 1 7
mknod -m 666 "$DEV/random" c 1 8
mknod -m 666 "$DEV/urandom" c 1 9
mknod -m 666 "$DEV/tty" c 5 0
# Devices bound into toolset (loop control, kvm), with the same numbers as on host.
for device in /dev/loop-control /dev/kvm; do
    [ -e "$device" ] || continue
    mknod -m 660 "$DEV/$(basename "$device")" c $((0x$(stat -c %t "$device"))) $((0x$(stat -c %T "$device")))
done
# Loop devices (block major 7). Catalyst gets free loop device from loop control, which can be created by kernel only
# now (eg. when others are used by mounted toolset), so its node wouldn't exist in host /dev bound earlier.
for number in $(seq 0 255); do
    mknod -m 660 "$DEV/loop$number" b 7 $number
done
mkdir -p "$DEV/pts" "$DEV/shm"
ln -s /proc/self/fd "$DEV/fd"
ln -s /proc/self/fd/0 "$DEV/stdin"
ln -s /proc/self/fd/1 "$DEV/stdout"
ln -s /proc/self/fd/2 "$DEV/stderr"
ln -s pts/ptmx "$DEV/ptmx"
mount --bind "$DEV" /dev
mount -t devpts -o newinstance,ptmxmode=0666,mode=0620 devpts /dev/pts
mount -t tmpfs -o mode=1777,nosuid,nodev shm /dev/shm
set +e
"""

def _strip_archive_extension(path: str) -> str:
    for extension in STAGE_ARCHIVE_EXTENSIONS:
        if path.endswith(extension):
            return path[:-len(extension)]
    return path

def _spec_values(spec: str) -> dict[str, str]:
    """Single line values of generated spec."""
    return {key.strip(): value.strip() for line in spec.splitlines() if ":" in line and not line.startswith("\t") for key, value in [line.split(":", 1)]}

@root_function
def prepare_loop_devices() -> list[str]:
    """Makes sure there is unused loop device and returns loop control and loop devices paths."""
    import subprocess, glob, re
    # Finding first unused loop device creates its node if all existing ones are in use.
    subprocess.run(["losetup", "--find"], check=True, stdout=subprocess.DEVNULL)
    devices = sorted(glob.glob("/dev/loop[0-9]*"), key=lambda path: int(re.sub(r"\D", "", path)))
    return (["/dev/loop-control"] if os.path.exists("/dev/loop-control") else []) + devices

def _is_build_file(filename: str, name: str, iso_name: str | None) -> bool:
    """Files of build: archive of stage with DIGESTS, CONTENTS..., and ISO of livecd with its files."""
    return filename.startswith(name + ".") or (iso_name is not None and (filename == iso_name or filename.startswith(iso_name + ".")))

def _move_stage_build_files(source_directory: str, name: str, destination_directory: str,
                            iso_name: str | None = None, owner_uid: int | None = None) -> list[str]:
    """Moves catalyst outputs, optionally assigning them to the build owner."""
    os.makedirs(destination_directory, exist_ok=True)
    moved = []
    if not os.path.isdir(source_directory):
        return moved
    for filename in sorted(os.listdir(source_directory)):
        if _is_build_file(filename, name, iso_name):
            target = os.path.join(destination_directory, filename)
            shutil.move(os.path.join(source_directory, filename), target)
            if owner_uid is not None:
                os.chown(target, owner_uid, owner_uid)
            moved.append(filename)
    # Folder of target left by catalyst (eg. root of livecd ISO, already in ISO and tarball).
    leftover = os.path.join(source_directory, name)
    if moved and os.path.isdir(leftover) and not os.path.islink(leftover):
        shutil.rmtree(leftover, ignore_errors=True)
    return moved

@root_function
def move_stage_build_files(source_directory: str, name: str, destination_directory: str, iso_name: str | None = None) -> list[str]:
    """Moves files produced by catalyst for given build name (archive, DIGESTS, CONTENTS..., ISO of livecd) to
    destination, and makes them owned by user. Folder of target left by catalyst (root of livecd ISO) is removed."""
    uid = RootHelperServer.shared().uid
    return _move_stage_build_files(source_directory, name, destination_directory, iso_name, owner_uid=uid)
