from __future__ import annotations
import os, uuid, shutil, threading, stat, re, json
import re, copy
from typing import final, Any
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from collections import namedtuple
from .root_function import root_function, local_for_rootless_paths
from .runtime_env import RuntimeEnv
from .event_bus import EventBus, SharedEvent
from .root_helper_server import ServerResponse, ServerResponseStatusCode
from .hotfix_patching import HotFix, apply_patch_and_store_for_isolated_system
from .repository import Serializable, Repository
from .toolset_application import ToolsetApplication, ToolsetApplicationInstall
from .helper_functions import create_temp_workdir, delete_temp_workdir, mount_squashfs, umount_squashfs, create_squashfs, loop_mount_squashfs, loop_umount_squashfs
from .status_indicator import StatusIndicatorState, StatusIndicatorValues, StatusDetail, item_status
from .rootless import (
    rootless_toolset_unsupported_reason, run_in_namespace, extracted_squashfs, writable_squashfs_copy,
    remove_in_namespace, squashfs_process, sessions_directory, executor_for_machine, LOCAL_EXECUTOR,
    _filesystem_operations
)

class ToolsetEvents(Enum):
    SPAWNED_CHANGED = auto()
    IN_USE_CHANGED = auto()
    IS_RESERVED_CHANGED = auto()

@final
class Toolset(Serializable):
    """Class containing details of the Toolset instances."""
    """Only metadata, no functionalities."""
    """Functionalities are handled by ToolsetContainer."""
    def __init__(self, env: ToolsetEnv, uuid: UUID, name: str, metadata: dict[str, Any] = {}, squashfs_binding_dir: str | None = None, machine_id: uuid.UUID | None = None, **kwargs):
        self.uuid = uuid
        self.env = env
        self.name = name
        self.metadata = metadata
        self.machine_id = machine_id # Virtual machine where toolset runs, None for this computer.
        self.squashfs_binding_dir = squashfs_binding_dir # Directory used as toolset_root, mounted when setting up or spawning.
        self.squashfs_loop_mounted = False # squashfs_binding_dir is read only loop mount of squashfs file (not extracted copy).
        match env:
            case ToolsetEnv.SYSTEM:
                pass
            case ToolsetEnv.EXTERNAL:
                pass
            case _:
                raise ValueError(f"Unknown env: {env}")
        self.access_lock = threading.RLock()
        self.spawned = False # Spawned means that directories in /tmp are prepared to be used with bwrap.
        self.in_use = False # Is any bwrap instance currently running on this toolset.
        self.is_reserved = False # Reserved for later usage by some object
        # Current spawn settings:
        self.store_changes: bool = False
        self.bind_options: list[str] | None = None # Binding options prepared in current spawn for bwrap command.
        self.current_bindings: list[BindMount] | None = None
        self.additional_bindings: list[BindMount] | None = None
        self.hot_fixes: list[HotFix] | None = None
        self.work_dir: str | None = None
        # Rootless spawn runs bwrap in user namespace instead of root helper. Toolset root is then extracted copy of
        # squashfs: shared read only copy, or writable copy (rootless_writable_copy) removed when unspawning.
        self.rootless = False
        self.rootless_writable_copy: str | None = None
        self.event_bus = EventBus[ToolsetEvents]()

    @property
    def short_details(self) -> str:
        app_strings: [str] = []
        for app in ToolsetApplication.ALL:
            if app.auto_select:
                continue
            app_install = self.get_app_install(app=app)
            if app_install:
                app_strings.append(f"{app.name}: {app_install.version}")
        if app_strings:
            return ", ".join(app_strings)
        else:
            return ""

    @property
    def status_indicator_values(self) -> StatusIndicatorValues:
        """Mounted (environment open, also with changes kept) is loaded. Missing file or virtual machine is error,
        missing Catalyst is warning (projects can't be built with toolset). Blinks while used by operation."""
        machine = self.machine
        details = []
        if not os.path.exists(self.file_path()):
            details.append(StatusDetail(StatusIndicatorState.ERROR, "File of toolset is missing"))
        if self.machine_id and (machine is None or machine.status == machine.STATUS_MISSING):
            details.append(StatusDetail(StatusIndicatorState.ERROR, "Virtual machine of toolset is missing"))
        if self.get_app_install(ToolsetApplication.CATALYST) is None:
            details.append(StatusDetail(StatusIndicatorState.WARNING, "Catalyst is not installed, projects can't be built"))
        if self.spawned:
            details.append(StatusDetail(StatusIndicatorState.LOADED, "Environment is open" + (", changes are kept" if self.store_changes else "")))
        if self.in_use:
            details.append(StatusDetail(StatusIndicatorState.LOADED, "Running command"))
        elif self.is_reserved:
            details.append(StatusDetail(StatusIndicatorState.LOADED, "Used by operation"))
        if not details:
            details.append(StatusDetail(StatusIndicatorState.IDLE, "Not mounted"))
        return item_status(details, blinking=self.in_use or self.is_reserved)

    @classmethod
    def init_from(cls, data: dict) -> Toolset:
        try:
            uuid_value = uuid.UUID(data["uuid"])
            env = ToolsetEnv[data["env"]]
            name = str(data["name"])
            metadata = data.get("metadata", {})
            machine_id = uuid.UUID(data["machine_id"]) if data.get("machine_id") else None
        except KeyError:
            raise ValueError(f"Failed to parse {data}")
        kwargs = {}
        match env:
            # Additional data only for given type (set as kwargs):
            case ToolsetEnv.SYSTEM:
                pass
            case ToolsetEnv.EXTERNAL:
                pass
        return cls(env, uuid_value, name, metadata, None, machine_id=machine_id, **kwargs)

    def serialize(self) -> dict:
        data = {
            "uuid": str(self.uuid),
            "env": self.env.name,
            "name": self.name,
            "metadata": self.metadata
        }
        if self.machine_id:
            data["machine_id"] = str(self.machine_id)
        return data

    @property
    def machine(self):
        """Virtual machine where toolset runs, None when it runs on this computer."""
        from .build_machine import build_machine_for_id
        return build_machine_for_id(self.machine_id)

    @property
    def executor(self):
        """Runs rootless commands of toolset, on this computer or in its machine."""
        return executor_for_machine(self.machine)

    @property
    def busy_reason(self) -> str | None:
        """Why toolset can't be used for new operation now (build, update...), or None when it's free."""
        if self.is_reserved:
            return f"Toolset {self.name} is used by another operation"
        if self.spawned:
            return f"Environment of toolset {self.name} is open, close it first"
        if self.machine_id and self.machine is None:
            return f"Virtual machine of toolset {self.name} was removed"
        machine = self.machine
        if machine and machine._status in (machine.STATUS_STARTING, machine.STATUS_STOPPING):
            return f"Virtual machine {machine.name} is {machine._status.lower()}"
        if machine and machine._status == machine.STATUS_MISSING:
            return f"Virtual machine {machine.name} doesn't exist"
        return None

    @property
    def runs_on_name(self) -> str:
        machine = self.machine
        return machine.name if machine else "This computer"

    @staticmethod
    def create_system() -> Toolset:
        """Create a Toolset with the SYSTEM environment."""
        return Toolset(ToolsetEnv.SYSTEM, uuid.uuid4(), "Host system")

    @staticmethod
    def create_external(name: str) -> Toolset:
        """Create a Toolset with the EXTERNAL environment and a specified squashfs file."""
        return Toolset(ToolsetEnv.EXTERNAL, uuid.uuid4(), name)

    def is_allowed_in_current_host() -> bool:
        return self.env.is_running_in_gentoo_host()

    def toolset_root(self) -> str | None:
        match self.env:
            case ToolsetEnv.SYSTEM:
                return "/"
            case ToolsetEnv.EXTERNAL:
                return self.squashfs_binding_dir

    # --------------------------------------------------------------------------
    # Spawning cycle:

    def spawn(self, store_changes: bool = False, hot_fixes: list[HotFix] | None = None, additional_bindings: list[BindMount] | None = None):
        """Prepare /tmp folders for bwrap calls. Toolsets of machines use working space of machine while spawned."""
        machine = self.machine
        if machine:
            self._workspace_user = f"Toolset {self.name} mounted"
            machine.acquire_workspace(user=self._workspace_user)
            self._workspace_machine = machine
        try:
            self._spawn(store_changes=store_changes, hot_fixes=hot_fixes, additional_bindings=additional_bindings)
        finally:
            # Released by unspawn when spawning failed after toolset was spawned.
            if not self.spawned and getattr(self, "_workspace_machine", None):
                self._workspace_machine.release_workspace(user=self._workspace_user)
                self._workspace_machine = None

    def _spawn(self, store_changes: bool = False, hot_fixes: list[HotFix] | None = None, additional_bindings: list[BindMount] | None = None):
        with self.access_lock:
            if not self.is_reserved:
                raise RuntimeError(f"Please reserve before calling commands.")
            if self.spawned:
                raise RuntimeError(f"Toolset {self} already spawned.")

            # Prepare /tmp directories and bind_options
            runtime_env = RuntimeEnv.current()
            machine = self.machine
            if self.machine_id and machine is None:
                raise RuntimeError("Virtual machine of toolset was removed")
            if hot_fixes and machine:
                raise RuntimeError("Hot fixes are not supported in virtual machines")
            # Toolsets of machines always run without root, in machine.
            self.rootless = machine is not None or (self.env == ToolsetEnv.EXTERNAL and rootless_toolset_unsupported_reason() is None)
            executor = self.executor

            # Create squashfs mounting if needed. Toolset being installed already has its root directory.
            uses_squashfs_file = self.squashfs_binding_dir is None and self.file_path() and os.path.exists(self.file_path())
            if uses_squashfs_file and self.rootless:
                if store_changes:
                    self.squashfs_binding_dir = writable_squashfs_copy(squashfs_path=self.file_path(), output_handler=print, executor=executor)
                    self.rootless_writable_copy = self.squashfs_binding_dir
                else:
                    self.squashfs_binding_dir = extracted_squashfs(squashfs_path=self.file_path(), kind="toolsets", output_handler=print, check_path="usr", executor=executor)
                self.squashfs_loop_mounted = False
            elif uses_squashfs_file:
                prefix = f"toolsets/{Toolset.sanitized_name_for_name(name=self.name)}/mount_"
                if store_changes:
                    # Changes are written directly to toolset files and squashfs is rebuilt from them when unspawning,
                    # so it needs writable copy.
                    self.squashfs_binding_dir = mount_squashfs(squashfs_path=self.file_path(), prefix=prefix)
                    self.squashfs_loop_mounted = False
                else:
                    # Read only mount, writes go to overlays created below. Much faster than extracting.
                    self.squashfs_binding_dir = loop_mount_squashfs(squashfs_path=self.file_path(), prefix=prefix)
                    self.squashfs_loop_mounted = True

            # Paths of machine toolsets are in machine, they are checked there.
            resolved_toolset_root = self.toolset_root() if machine else str(Path(self.toolset_root()).resolve())
            if resolved_toolset_root == "/" and store_changes:
                self._release_squashfs_binding_dir()
                raise RuntimeError("Cannot use store_changes with host toolset")
            if executor.filesystem({"info": [resolved_toolset_root]})["info"][resolved_toolset_root]["type"] != "dir":
                self._release_squashfs_binding_dir()
                raise RuntimeError(f"Toolset root directory not found: {resolved_toolset_root}")

            _system_bindings = [ # System.
                BindMount(mount_path="/usr",   toolset_path="/usr",   store_changes=store_changes),
                BindMount(mount_path="/bin",   toolset_path="/bin",   store_changes=store_changes),
                BindMount(mount_path="/sbin",  toolset_path="/sbin",  store_changes=store_changes),
                BindMount(mount_path="/lib",   toolset_path="/lib",   store_changes=store_changes),
                BindMount(mount_path="/lib32", toolset_path="/lib32", store_changes=store_changes),
                BindMount(mount_path="/lib64", toolset_path="/lib64", store_changes=store_changes),
            ]
            _devices_bindings = [ # Devices.
                BindMount(mount_path="/dev/kvm", host_path="/dev/kvm", store_changes=True) # Store changes is added only to use --dev-bind flag
            ]
            _config_bindings = [ # Config.
                BindMount(mount_path="/etc", toolset_path="/etc", store_changes=store_changes),
                BindMount(mount_path="/etc/resolv.conf", host_path="/etc/resolv.conf"), # Take resolv.conf directly from main system
            ]
            _working_bindings = [ # Working.
                BindMount(mount_path="/var", toolset_path="/var", store_changes=store_changes),
                # Work/tmp/cache directories that should always be stored in temporary directory, not in the real toolset.
                BindMount(mount_path="/tmp", create_if_missing=True),
                BindMount(mount_path="/var/tmp", create_if_missing=True),
                BindMount(mount_path="/var/cache", create_if_missing=True),
                # Set portage owner
                BindMount(mount_path="/var/tmp/portage", create_if_missing=True, owner="portage:portage"),
                BindMount(mount_path="/var/cache/distfiles", create_if_missing=True, owner="portage:portage"),
                BindMount(mount_path="/var/cache/binpkgs", create_if_missing=True, owner="portage:portage"),
                # Uncomment if portage tree should not be kept in squashfs
                #BindMount(mount_path="/var/db/repos", create_if_missing=True),
            ]
            # All bindings.
            bindings = ( _system_bindings + _config_bindings + _devices_bindings + _working_bindings + (additional_bindings or []) )

            # Map bindings using toolset_path to host_path.
            for bind in bindings:
                if bind.owner and bind.host_path:
                    raise ValueError(f"Can set owner of host binding: {bind.host_path}")
                if bind.host_path and bind.toolset_path:
                    raise ValueError(f"BindMount for mount_path '{bind.mount_path}' has both host_path and toolset_path set. Only one is allowed.")
                if bind.toolset_path:
                    bind.host_path = os.path.join(resolved_toolset_root, bind.toolset_path.lstrip("/"))
                # Resolve host_path.
                if bind.host_path:
                    bind.host_path = os.path.expanduser(bind.host_path)

            bind_options = []
            work_dir: str | None = None
            # Directories and symlinks are created on this computer right away, in machine all at once at the end.
            pending_directories: list[str] = []
            pending_symlinks: list[list[str]] = []
            def make_directory(path: str):
                if machine:
                    pending_directories.append(path)
                else:
                    os.makedirs(path, exist_ok=False)
            def make_symlink(target: str, path: str):
                if machine:
                    pending_symlinks.append([target, path])
                else:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    os.symlink(target, path)
            try:
                OverlayPaths = namedtuple("OverlayPaths", ["upper", "work"])
                if machine:
                    work_dir = os.path.join(sessions_directory(executor), f"toolset-{uuid.uuid4().hex}")
                elif self.rootless:
                    work_dir = os.path.join(sessions_directory(), f"toolset-{uuid.uuid4().hex}")
                    os.makedirs(work_dir)
                else:
                    work_dir = create_temp_workdir(prefix=f"toolsets/{Toolset.sanitized_name_for_name(name=self.name)}/bwrap_")

                # Prepare work dirs:
                fake_root = os.path.join(work_dir, "fake_root")
                overlay_root = os.path.join(work_dir, "overlay")
                hotfixes_workdir = os.path.join(work_dir, "hotfixes") # Stores patched files if needed
                if machine:
                    make_directory(work_dir)
                make_directory(fake_root)
                make_directory(overlay_root)
                for field in OverlayPaths._fields: # Creates upper and work subdirectories.
                    make_directory(os.path.join(overlay_root, field))
                make_directory(hotfixes_workdir)

                # Collect required hotfix patched files and add to bindings:
                hotfix_patches = [fix.get_patch_spec for fix in (hot_fixes or [])]
                for patch in hotfix_patches:
                    patched_file_path = apply_patch_and_store_for_isolated_system(runtime_env, resolved_toolset_root, hotfixes_workdir, patch)
                    if patched_file_path is not None:
                        # Convert patch file to BindMount structure
                        patched_file_binding = BindMount(mount_path=patch.source_path, host_path=patched_file_path, resolve_host_path=False)
                        bindings.append(patched_file_binding)

                # Name overlay entries using indexes to avoid overlaps.
                mapping_index=0

                # Creates entry in overlay that maps other directory.
                def create_overlay_map(mount_path: str) -> OverlayPaths:
                    nonlocal mapping_index
                    # Create directories for all fields in OverlayPaths with given mount_path (upper, work [lower is considered mapped directory])
                    map_name=mount_path.replace("/", "_")
                    values = {
                        field: f"{overlay_root}/{field}/{mapping_index}{map_name}".replace("//", "/")
                        for field in OverlayPaths._fields
                    }
                    for path in values.values():
                        make_directory(path)
                    mapping_index+=1
                    return OverlayPaths(**values)
                # Creates entry in overlay for temp dir, without mapping other directory.
                def create_overlay_temp(mount_path: str) -> str:
                    nonlocal mapping_index
                    map_name=mount_path.replace("/", "_")
                    overlay_mount_path=f"{overlay_root}/upper/{mapping_index}{map_name}".replace("//", "/")
                    make_directory(overlay_mount_path)
                    mapping_index+=1
                    return overlay_mount_path

                # Types of binding sources, checked where toolset runs (through runtime env on this computer, it works
                # with flatpak env).
                def resolved_path(binding: BindMount) -> str | None:
                    if binding.host_path is None:
                        return None
                    if machine or not binding.resolve_host_path:
                        return binding.host_path
                    return runtime_env.resolve_path_for_host_access(binding.host_path)
                source_paths = [path for path in (resolved_path(binding) for binding in bindings) if path]
                path_info = executor.filesystem({"info": source_paths})["info"]

                # Bind files and directories specified in bindings inside fake_root:
                for binding in bindings[:]:
                    resolved_host_path = resolved_path(binding)
                    kind = path_info.get(resolved_host_path, {}).get("type") if resolved_host_path else None
                    # Handle not existing host paths.
                    if binding.host_path is not None and kind is None:
                        # Create in host if store_changes is set.
                        if binding.create_if_missing and binding.store_changes: # TODO: Think this logic through
                            print(f"Path {resolved_host_path} not found. Creating directory in host.")
                            if machine:
                                pending_directories.append(resolved_host_path)
                            else:
                                os.makedirs(resolved_host_path)
                            kind = "dir"
                        # Skip not existing bindings with host_path set:
                        else:
                            print(f"Path {resolved_host_path} not found. Skipping binding.")
                            bindings.remove(binding)
                            continue # or raise an error if that's preferred

                    # Empty writable dirs:
                    if binding.host_path is None:
                        tmp_path = create_overlay_temp(binding.mount_path)
                        bind_options.extend(["--bind", tmp_path, binding.mount_path])
                        continue
                    # Symlinks (keep as symlinks in isolated env):
                    if kind == "link":
                        fake_symlink_path = os.path.join(fake_root, binding.mount_path.lstrip("/"))
                        make_symlink(path_info[resolved_host_path]["target"], fake_symlink_path)
                        continue
                    # Char and block devices (eg. /dev/kvm, /dev/loopN):
                    if kind in ("chr", "blk"):
                        flag = "--dev-bind" if binding.store_changes else "--ro-bind"
                        bind_options.extend([flag, binding.host_path, binding.mount_path])
                        continue
                    # Standard files:
                    if kind == "file":
                        flag = "--bind" if binding.store_changes else "--ro-bind"
                        bind_options.extend([flag, binding.host_path, binding.mount_path])
                        continue
                    # Directories:
                    if kind == "dir":
                        if binding.store_changes:
                            bind_options.extend(["--bind", binding.host_path, binding.mount_path])
                        else:
                            overlay = create_overlay_map(binding.mount_path)
                            bind_options.extend([
                                "--overlay-src", binding.host_path,
                                "--overlay", overlay.upper, overlay.work, binding.mount_path
                            ])
                        continue
                if machine:
                    executor.filesystem({"mkdir": pending_directories, "symlink": pending_symlinks})
            except Exception as e:
                error = e
                try:
                    if work_dir:
                        self._delete_work_dir(work_dir)
                    self._release_squashfs_binding_dir()
                except Exception as e2:
                    error = ExceptionGroup("Multiple errors spawning environment", [error, e2])
                raise error

            self.work_dir = work_dir
            self.hot_fixes = hot_fixes
            self.current_bindings = bindings
            self.additional_bindings = additional_bindings
            self.store_changes = store_changes
            self.bind_options = bind_options
            self.spawned = True

            # Set bindings owners if needed
            try:
                if not self._run_command_and_wait("echo Hello World"):
                    raise RuntimeError("Toolset test failed")
                chmod_commands = [
                    f"chown -R {binding.owner} {binding.mount_path}"
                    for binding in bindings
                    if binding.owner is not None
                ]
                if chmod_commands:
                    if not self._run_command_and_wait(" && ".join(chmod_commands)):
                        raise RuntimeError("Toolset test failed")
                self.event_bus.emit(ToolsetEvents.SPAWNED_CHANGED, self.spawned)
                self.event_bus.emit(SharedEvent.STATE_UPDATED, self)
            except Exception as e:
                print(e)
                self.unspawn(rebuild_squashfs_if_needed=False)

    def _run_command_and_wait(self, command: str) -> bool:
        """Runs command in spawned toolset, used while spawning."""
        if self.rootless:
            return run_in_namespace(self._bwrap_script(command), print, executor=self.executor)
        fake_root = os.path.join(self.work_dir, "fake_root")
        result = _start_toolset_command._raw(work_dir=self.work_dir, fake_root=fake_root, bind_options=self.bind_options, command_to_run=command)
        return result.code == ServerResponseStatusCode.OK

    def _bwrap_script(self, command: str) -> str:
        """Command running in rootless toolset, same environment as _start_toolset_command."""
        import shlex
        fake_root = os.path.join(self.work_dir, "fake_root")
        options = " ".join(shlex.quote(option) for option in self.bind_options)
        return (
            f"exec bwrap --die-with-parent --unshare-uts --unshare-ipc --unshare-pid --unshare-cgroup "
            f"--hostname catalyst-lab --bind {shlex.quote(fake_root)} / --dev /dev --proc /proc "
            f"--setenv HOME / --setenv LANG C.UTF-8 --setenv LC_ALL C.UTF-8 {options} bash -c {shlex.quote(command)} < /dev/null"
        )

    def _delete_work_dir(self, work_dir: str):
        if self.rootless:
            remove_in_namespace([work_dir], print, executor=self.executor)
        else:
            delete_temp_workdir(path=work_dir)

    def _release_squashfs_binding_dir(self):
        """Unmounts or deletes squashfs contents prepared by spawn, used when spawning fails."""
        if self.rootless:
            if self.rootless_writable_copy:
                remove_in_namespace([self.rootless_writable_copy], print, executor=self.executor)
            self.rootless_writable_copy = None
            self.squashfs_binding_dir = None
            return
        if self.squashfs_binding_dir and self.file_path() and os.path.exists(self.file_path()):
            if self.squashfs_loop_mounted:
                loop_umount_squashfs(mount_point=self.squashfs_binding_dir)
            else:
                umount_squashfs(mount_point=self.squashfs_binding_dir)
            self.squashfs_binding_dir = None
            self.squashfs_loop_mounted = False

    def unspawn(self, rebuild_squashfs_if_needed: bool = True, clean_squashfs_binding_dir: bool = True):
        """Clear tmp folders."""
        with self.access_lock:
            if not self.is_reserved:
                raise RuntimeError(f"Please reserve before calling commands.")
            if not self.spawned:
                raise RuntimeError(f"Toolset {self} is not spawned.")
            if self.in_use:
                raise RuntimeError(f"Toolset {self} is currently in use.")
            try:
                if rebuild_squashfs_if_needed and self.store_changes and self.file_path():
                    create_squashfs_process = self.create_squashfs(output_file=self.file_path()+"_tmp")
                    for _ in create_squashfs_process.stdout:
                        pass
                    create_squashfs_process.wait()
                    if os.path.isfile(self.file_path()+"_tmp"):
                        shutil.move(self.file_path()+"_tmp", self.file_path())
                if self.rootless:
                    # Shared read only copy is kept, writable one is removed.
                    if clean_squashfs_binding_dir:
                        self._release_squashfs_binding_dir()
                elif self.squashfs_binding_dir and clean_squashfs_binding_dir:
                    if self.squashfs_loop_mounted:
                        loop_umount_squashfs(mount_point=self.squashfs_binding_dir)
                    else:
                        umount_squashfs(mount_point=self.squashfs_binding_dir)
                if self.work_dir:
                    self._delete_work_dir(self.work_dir)
                # Reset spawned settings:
                self.squashfs_binding_dir = None
                self.squashfs_loop_mounted = False
                self.work_dir = None
                self.hot_fixes = None
                self.current_bindings = None
                self.additional_bindings = None
                self.store_changes = False
                self.bind_options = None
                self.spawned = False
                self.rootless = False
                self.rootless_writable_copy = None
                if getattr(self, "_workspace_machine", None):
                    self._workspace_machine.release_workspace(user=self._workspace_user)
                    self._workspace_machine = None
                self.event_bus.emit(ToolsetEvents.SPAWNED_CHANGED, self.spawned)
                self.event_bus.emit(SharedEvent.STATE_UPDATED, self)
            except Exception as e:
                print(f"Error deleting toolset work_dir: {e}")
                raise e

    # --------------------------------------------------------------------------
    # Reserving:

    def reserve(self) -> bool:
        with self.access_lock:
            if self.is_reserved:
                return False
            else:
                self.is_reserved = True
                self.event_bus.emit(ToolsetEvents.IS_RESERVED_CHANGED, self.is_reserved)
                self.event_bus.emit(SharedEvent.STATE_UPDATED, self)
                return True

    def release(self) -> bool:
        with self.access_lock:
            if not self.is_reserved:
                return False
            self.is_reserved = False
            self.event_bus.emit(ToolsetEvents.IS_RESERVED_CHANGED, self.is_reserved)
            self.event_bus.emit(SharedEvent.STATE_UPDATED, self)
            return True

    # --------------------------------------------------------------------------
    # Calling commands:

    def run_command(self, command: str, handler: callable | None = None, completion_handler: callable | None = None) -> ServerCall:
        # TODO: Add required parameters checks, like store_changes matches spawned env, required bindings are set correctly etc.
        with self.access_lock:
            if not self.is_reserved:
                raise RuntimeError(f"Please reserve before calling commands.")
            if not self.spawned:
                raise RuntimeError(f"Toolset {self} is not spawned.")
            if self.in_use:
                raise RuntimeError(f"Toolset {self} is currently in use.")
            self.in_use = True
            self.event_bus.emit(ToolsetEvents.IN_USE_CHANGED, self.in_use)
            self.event_bus.emit(SharedEvent.STATE_UPDATED, self)

            def on_complete(completion_handler: callable | None, result: ServerResponse):
                with self.access_lock:
                    self.in_use = False
                    self.event_bus.emit(ToolsetEvents.IN_USE_CHANGED, self.in_use)
                    self.event_bus.emit(SharedEvent.STATE_UPDATED, self)
                if completion_handler:
                    try:
                        completion_handler(result)
                    except Exception as e:
                        print(f"Completion handler raised exception: {e}")
            try:
                if self.rootless:
                    return NamespaceCall(script=self._bwrap_script(command), executor=self.executor, handler=handler,
                                         completion_handler=lambda x: on_complete(completion_handler, x))
                fake_root = os.path.join(self.work_dir, "fake_root")
                return _start_toolset_command._async_raw(
                    handler=handler,
                    # Wraps completion block to set in_use flag additionally after it's done
                    completion_handler=lambda x: on_complete(completion_handler, x),
                    work_dir=self.work_dir,
                    fake_root=fake_root,
                    bind_options=self.bind_options,
                    command_to_run=command
                )
            except Exception as e:
                print(f"Failed to execute command: {e}")
                self.in_use = False
                self.event_bus.emit(ToolsetEvents.IN_USE_CHANGED, self.in_use)
                self.event_bus.emit(SharedEvent.STATE_UPDATED, self)
                raise e

    # --------------------------------------------------------------------------
    # Managing installed apps:

    def get_app_install(self, app: ToolsetApplication) -> ToolsetApplicationInstall | None:
        app_metadata = self.metadata.get(app.package, {})
        if not app_metadata:
            return None
        patches = app_metadata.get('patches', [])
        version = app_metadata.get('version')
        version_id = app_metadata.get('version_id')
        if not version:
            return None
        if version_id:
            version_id_uuid = uuid.UUID(version_id)
            version_variant = next((version for version in app.versions if version.id == version_id_uuid), None)
            return ToolsetApplicationInstall(version=version, variant=version_variant, patches=patches)
        else:
            version_variant = app.versions[0]
            return ToolsetApplicationInstall(version=version, variant=version_variant, patches=patches)

    def analyze(self, save: bool = False) -> dict[str, Any] | None:
        """Performs various sanity checks on toolset and stores gathered results."""
        """Returns true if all checks succeeded, even if version is not found."""
        metadata_copy = copy.deepcopy(self.metadata) if self.metadata else {}
        checks_succeded = True
        for app in ToolsetApplication.ALL:
            try:
                self._perform_app_installed_version_check(app=app, metadata=metadata_copy)
            except Exception as e:
                print(f"Error in installed version check: {e}")
                checks_succeeded = False
            try:
                self._perform_app_additional_checks(app=app, metadata=metadata_copy)
            except Exception as e:
                print(f"Error in additional checks: {e}")
                checks_succeeded = False
        if checks_succeded and save:
            self.replace_metadata(metadata_copy)
        return metadata_copy if checks_succeded else None

    def replace_metadata(self, metadata: dict[str, Any] | None):
        """Updates whole metadata dictionary and emits event."""
        self.metadata = metadata
        if self.store_changes:
            self.write_metadata_to_json(metadata=metadata)
        Repository.Toolset.save() # Make sure changes are saved in repository.
        self.event_bus.emit(SharedEvent.STATE_UPDATED, self)

    def write_metadata_to_json(self, metadata: dict[str, Any] | None):
        if self.machine:
            path = os.path.join(self.toolset_root(), "toolset.json")
            self.executor.filesystem({"write": [[path, json.dumps(metadata, indent=4)]]} if metadata is not None else {"remove": [path]})
            return
        write_metadata_to_json(toolset_root=self.toolset_root(), metadata=metadata)

    # Files of toolset, used by its processes. Toolset root can be in machine, so they go through toolset executor.

    def create_squashfs(self, output_file: str):
        """Process packing toolset root to squashfs file, with output like mksquashfs -percentage."""
        if self.rootless or self.machine:
            return squashfs_process(source_directory=self.toolset_root(), output_file=output_file, executor=self.executor)
        return create_squashfs(source_directory=self.toolset_root(), output_file=output_file)

    def write_portage_files(self, files: dict[str, str]):
        """Writes files (path relative to /etc/portage: content) to toolset."""
        if self.machine:
            root = os.path.join(self.toolset_root(), "etc", "portage")
            self.executor.filesystem({"write": [[os.path.join(root, path), content] for path, content in files.items()]})
            return
        from .toolset_installation import insert_portage_file
        for path, content in files.items():
            insert_portage_file(relative_path=path, content=content, toolset_root=self.toolset_root())

    def remove_portage_files(self, paths: list[str]):
        """Removes files (paths relative to /etc/portage) from toolset."""
        if self.machine:
            root = os.path.join(self.toolset_root(), "etc", "portage")
            self.executor.filesystem({"remove": [os.path.join(root, path) for path in paths]})
            return
        from .toolset_update import remove_portage_file
        for path in paths:
            remove_portage_file(relative_path=path, toolset_root=self.toolset_root())

    def list_toolset_directory(self, relative_path: str) -> list[tuple[str, str]] | None:
        """Entries (name, dir/file/other) of directory in toolset, None if it doesn't exist."""
        path = os.path.join(self.toolset_root(), relative_path)
        entries = self.executor.filesystem({"list": [path]})["list"][path]
        return [tuple(entry) for entry in entries] if entries is not None else None

    def _perform_app_installed_version_check(self, app: ToolsetApplication, metadata: dict[str, Any]):
        app_metadata = metadata.setdefault(app.package, {})
        try:
            category, pkg_name = app.package.split("/", 1)
        except ValueError:
            category, pkg_name = None, None
        entries = self.list_toolset_directory(os.path.join("var", "db", "pkg", category)) if category else None
        if entries is None:
            app_metadata.pop("version", None)
            app_metadata.pop("patches", None)
            return
        # Version check
        package_regex = re.compile(
            rf"^{re.escape(pkg_name)}-(\d+(?:\.\d+)*(_(?:alpha|beta|pre|rc|p)\d*)?(-r\d+)?)$"
        )
        version_value = next((match.group(1) for name, kind in entries if kind == "dir" and (match := package_regex.match(name))), None)
        # Patch check (patches can be in subdirectories for given versions)
        patch_files = []
        directories = [os.path.join("etc", "portage", "patches", category, pkg_name)]
        while directories:
            directory = directories.pop()
            for name, kind in self.list_toolset_directory(directory) or []:
                if kind == "dir":
                    directories.append(os.path.join(directory, name))
                elif name.endswith(".patch"):
                    patch_files.append(name)
        # Set version and patch metadata
        if version_value:
            app_metadata["version"] = version_value
        else:
            app_metadata.pop("version", None)
        app_metadata["patches"] = patch_files

    def _perform_app_additional_checks(self, app: ToolsetApplication, metadata: dict[str, Any]):
        if app.toolset_additional_analysis:
            app.toolset_additional_analysis(app=app, toolset=self, metadata=metadata)

    @property
    def filename(self) -> str:
        return os.path.basename(self.file_path())

    def file_path(self) -> str:
        return Toolset.file_path_for_name(name=self.name)

    @staticmethod
    def file_path_for_name(name: str) -> str:
        file_name = Toolset.sanitized_name_for_name(name) + ".sqfs"
        return os.path.join(os.path.realpath(os.path.expanduser(Repository.Settings.value.toolsets_location)), file_name)

    @staticmethod
    def sanitized_name_for_name(name: str) -> str:
        def sanitize_filename_linux(name: str) -> str:
            return name.replace('/', '_').replace('\0', '_').replace(' ', '_')
        return sanitize_filename_linux(name=name)

class NamespaceCall:
    """Command running in rootless toolset. Has the same interface as ServerCall of root helper, so steps can use both."""

    def __init__(self, script: str, executor, handler: callable | None, completion_handler: callable | None):
        from .root_helper_client import ServerCallEvents
        self._new_output_line_event = ServerCallEvents.NEW_OUTPUT_LINE
        self.output: list[str] = []
        self.output_lock = threading.Lock()
        self.event_bus = EventBus()
        self.terminated = False
        self.handler = handler
        self.completion_handler = completion_handler
        self.script = script
        self.executor = executor
        self.processes = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            success = self.executor.run_in_namespace(self.script, self.output_append, self.processes)
        except Exception as e:
            self.output_append(f"Error: {e}")
            success = False
        code = ServerResponseStatusCode.OK if success else (
            ServerResponseStatusCode.JOB_WAS_TERMINATED if self.terminated else ServerResponseStatusCode.COMMAND_EXECUTION_FAILED)
        if self.completion_handler:
            self.completion_handler(ServerResponse(code=code))

    def output_append(self, line: str):
        with self.output_lock:
            self.output.append(line)
            self.event_bus.emit(self._new_output_line_event, line)
        if self.handler:
            self.handler(line)

    def get_output(self) -> list[str]:
        with self.output_lock:
            return self.output

    @property
    def is_cancellable(self) -> bool:
        return True

    def cancel(self):
        self.terminated = True
        for process in self.processes:
            process.terminate()

@dataclass
class BindMount:
    mount_path: str                 # Mount location inside the isolated environment.
    host_path: str | None = None    # None if mount point is an empty dir from overlay.
    toolset_path: str | None = None # Host path relative to toolset root.
    store_changes: bool = False     # True if changes should be stored outside isolated env.
    resolve_host_path: bool = True  # Whether to resolve path through runtime_env.
    create_if_missing: bool = False # Creates directory if not found on host.
    owner: str | None = None        # Sets owner of given file/dir. Works only with toolset_path or tmp bindings

@root_function
def _start_toolset_command(work_dir: str, fake_root: str, bind_options: list[str], command_to_run: str):
    import subprocess, shlex
    #subprocess.run(["chown", "-R", "root:root", work_dir], check=True) # This could change the ownership of work_dir for root, but probably is not needed.
    run_dir = RootHelperServer.get_runtime_dir(uid=RootHelperServer.shared().uid, runtime_env_name="CL_SERVER_RUNTIME_DIR")
    bwrap_path = os.path.join(run_dir, "bwrap")
    cmd_bwrap = (
        f"{shlex.quote(bwrap_path)} "
        "--die-with-parent "
        "--unshare-uts --unshare-ipc --unshare-pid --unshare-cgroup "
        "--hostname catalyst-lab "
        "--bind " + shlex.quote(fake_root) + " / "
        "--dev /dev "
        "--proc /proc "
        "--setenv HOME / "
        "--setenv LANG C.UTF-8 "
        "--setenv LC_ALL C.UTF-8 "
    )
    # Paths in bindings can contain spaces (eg. project names), quote them for shell.
    arguments_string = " ".join(shlex.quote(option) for option in bind_options) + " bash -c '" + command_to_run + "'"
    exec_call = cmd_bwrap + arguments_string
    print(exec_call)
    try:
        result = subprocess.run(exec_call, shell=True).returncode
        if result != 0:
            raise RuntimeError(f"Toolset call returned exit code: {result}")
    except Exception as e:
        # Note: We don't handle exceptions here, because if the root function throws,
        # the exception will be just returned as a result of this call, which is what we want.
        raise e

@final
class ToolsetEnv(Enum):
    SYSTEM   = auto() # Using tools from system, either through HOST or FLATPAK RuntimeEnv.
    EXTERNAL = auto() # Using tools from given .squashfs installation.

    def is_allowed_in_current_host(self) -> bool:
        """Can selected ToolsetEnv be used in current host. SYSTEM only allowed in gentoo, EXTERNAL allowed anywhere."""
        match self:
            case ToolsetEnv.SYSTEM:
                return RuntimeEnv.is_running_in_gentoo_host()
            case ToolsetEnv.EXTERNAL:
                return True

@root_function
def write_metadata_to_json(toolset_root: str, metadata: dict[str, Any] | None):
    # Save metadata result inside toolset json file
    json_file_path = os.path.join(toolset_root, "toolset.json")
    try:
        if metadata is None:
            if os.path.exists(json_file_path):
                os.remove(json_file_path)
        else:
            with open(json_file_path, 'w') as json_file:
                json.dump(metadata, json_file, indent=4)
    except Exception as e:
        print(f"Failed to write metadata to toolset: {e}")

write_metadata_to_json = local_for_rootless_paths(write_metadata_to_json, path_argument="toolset_root")
