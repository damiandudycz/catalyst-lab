from __future__ import annotations
import os, threading, uuid
from typing import Self
from .event_bus import EventBus, SharedEvent
from .repository import Repository
from .status_indicator import StatusIndicatorState, StatusIndicatorValues, StatusDetail, item_status, status_values
from .lima import list_instances, run_limactl, run_as_root_in_instance, swap_activation_script, default_lima_home, MACHINE_DATA_DIRECTORY, kill_instance_processes, instance_process_ids
from .lima import lima_home as default_new_lima_home

class BuildMachine:
    """Virtual machine (Lima instance) where toolsets run, on systems that can't build stages themselves (macOS).
    Toolsets linked to machine are spawned inside it, so their builds, updates and snapshots run there too."""

    STATUS_RUNNING = "Running"
    STATUS_STOPPED = "Stopped"
    STATUS_MISSING = "Missing"
    STATUS_STARTING = "Starting"   # Displayed while machine is being started or stopped by app.
    STATUS_STOPPING = "Stopping"

    # Delay before stopping unused machine, so it's not restarted between consecutive commands. Operations (builds,
    # environments, installations) use machine for their whole duration, so short delay is enough.
    IDLE_STOP_SECONDS = 2
    COMMAND_USER = "Command" # User of single commands (not listed as user of machine).

    DEFAULT_SWAP_GIB = 16

    def __init__(self, id: uuid.UUID | None = None, name: str = "", cpus: int = 4, memory_gib: int = 4, workspace_gib: int = 64,
                 lima_home: str | None = None, swap_gib: int = DEFAULT_SWAP_GIB):
        self.id = id or uuid.uuid4()
        self.name = name
        self.cpus = cpus
        self.memory_gib = memory_gib
        self.workspace_gib = workspace_gib # Maximum size of working space image (it grows only as it's used).
        self.swap_gib = swap_gib # Maximum size of swap disk (it grows only as it's used), 0 disables swap.
        self.lima_home = lima_home or default_new_lima_home() # Lima directory containing machine folder.
        self.event_bus = EventBus()
        self._status: str | None = None
        self._lock = threading.Lock()
        self._workspace_lock = threading.Lock()
        self._workspace_users = 0
        # Machines started automatically when used are stopped when nothing uses them (after IDLE_STOP_SECONDS).
        self._usage_lock = threading.Lock()
        self._users: list[str] = [] # Names of operations using machine, like reservations of toolsets.
        self._started_automatically = False
        self._stop_timer: threading.Timer | None = None

    @property
    def instance_name(self) -> str:
        """Name of Lima instance."""
        return f"catalystlab-{self.id.hex[:8]}" # Short, Lima socket paths are limited to 104 characters.

    def serialize(self) -> dict:
        return {"id": str(self.id), "name": self.name, "cpus": self.cpus, "memory_gib": self.memory_gib,
                "workspace_gib": self.workspace_gib, "swap_gib": self.swap_gib, "lima_home": self.lima_home}

    @classmethod
    def init_from(cls, data: dict) -> Self:
        return cls(id=uuid.UUID(data["id"]), name=data["name"], cpus=data.get("cpus", 4),
                   memory_gib=data.get("memory_gib", 4), workspace_gib=data.get("workspace_gib", 64),
                   swap_gib=data.get("swap_gib", cls.DEFAULT_SWAP_GIB),
                   # Machines created before machines directory are in ~/.lima.
                   lima_home=data.get("lima_home") or default_lima_home())

    # Status:

    @property
    def status(self) -> str:
        if self._status is None:
            self.refresh_status()
        return self._status

    def _set_status(self, status: str):
        if status != self._status:
            self._status = status
            self.event_bus.emit(SharedEvent.STATE_UPDATED, self)

    def refresh_status(self, during_transition: bool = False):
        """Reads status from Lima. While app starts or stops machine, its Starting or Stopping status is kept."""
        if self._status in (self.STATUS_STARTING, self.STATUS_STOPPING) and not during_transition:
            return
        instance = list_instances(self.lima_home).get(self.instance_name)
        status = instance.get("status", self.STATUS_STOPPED) if instance else self.STATUS_MISSING
        if status != self._status:
            self._status = status
            self.event_bus.emit(SharedEvent.STATE_UPDATED, self)

    @property
    def is_running(self) -> bool:
        return self.status == self.STATUS_RUNNING

    @property
    def short_details(self) -> str:
        swap = f", up to {self.swap_gib} GiB swap" if self.swap_gib else ""
        return f"{self.status} · {self.cpus} CPUs, {self.memory_gib} GiB memory{swap}, up to {self.workspace_gib} GiB working space"

    @property
    def status_indicator_values(self) -> StatusIndicatorValues:
        """Running is loaded, missing (removed outside of app) is error. Blinks while starting, stopping or used by
        operations."""
        match self.status:
            case self.STATUS_RUNNING:
                return item_status([StatusDetail(StatusIndicatorState.LOADED, "Running"),
                                    StatusDetail(StatusIndicatorState.LOADED, "Used by operation") if self.is_used else None],
                                   blinking=self.is_used)
            case self.STATUS_STARTING:
                return status_values(StatusIndicatorState.LOADED, blinking=True, description="Starting")
            case self.STATUS_STOPPING:
                return status_values(StatusIndicatorState.LOADED, blinking=True, description="Stopping")
            case self.STATUS_MISSING:
                return status_values(StatusIndicatorState.ERROR, description="Virtual machine is missing (removed outside of Catalyst Lab)")
        return status_values(StatusIndicatorState.IDLE, description="Stopped")

    # Usage:

    def read_usage(self) -> dict | None:
        """Counters of processors (from /proc/stat, usage is computed from two readings), memory, swap, load and disk
        usage of system disk and working space. None when machine doesn't run or can't be read."""
        from .lima import instance_output
        script = (f"head -n 1 /proc/stat; grep -c '^cpu[0-9]' /proc/stat; cat /proc/loadavg; "
                  f"grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo; "
                  f"df -kP / {MACHINE_DATA_DIRECTORY} 2>/dev/null")
        output = instance_output(self.instance_name, ["sh", "-c", script], home=self.lima_home)
        if output is None:
            return None
        try:
            lines = output.splitlines()
            cpu = [int(value) for value in lines[0].split()[1:9]]
            usage = {
                "cpu_total": sum(cpu), "cpu_idle": cpu[3] + cpu[4], "cpus": int(lines[1]),
                "load": float(lines[2].split()[0]), "disks": {},
            }
            for line in lines[3:]:
                if ":" in line and line.split(":")[0] in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                    key, value = line.split(":", 1)
                    usage[key] = int(value.split()[0]) * 1024
                elif len(parts := line.split()) >= 6 and parts[1].isdigit() and parts[5].startswith("/"):
                    # df: filesystem, size, used, available, capacity, mount point (KiB).
                    usage["disks"][parts[5]] = (int(parts[2]) * 1024, int(parts[1]) * 1024) # Used, size.
            return usage
        except (ValueError, IndexError):
            return None

    # Lifecycle:

    # Usage:
    # Operations using machine (mounted toolsets, builds, installations, single commands) register themselves while
    # they run. Machine started automatically for them is stopped when nothing uses it anymore (after short delay, so
    # it's not restarted between consecutive commands). Machine started by user keeps running.

    @property
    def users(self) -> list[str]:
        """Operations using machine, without single commands."""
        return [user for user in self._users if user != self.COMMAND_USER]

    @property
    def is_used(self) -> bool:
        return bool(self._users)

    @property
    def stops_when_unused(self) -> bool:
        return self._started_automatically

    @property
    def stop_scheduled(self) -> bool:
        return self._stop_timer is not None

    def begin_use(self, user: str = COMMAND_USER):
        """Marks machine as used by operation, cancels its automatic stop. Call end_use with the same name later."""
        with self._usage_lock:
            self._users.append(user)
            if self._stop_timer:
                self._stop_timer.cancel()
                self._stop_timer = None
        self.event_bus.emit(SharedEvent.STATE_UPDATED, self)

    def end_use(self, user: str = COMMAND_USER):
        """Marks end of usage. Machine started automatically is stopped when nothing uses it."""
        with self._usage_lock:
            if user in self._users:
                self._users.remove(user)
            if not self._users and self._started_automatically and self._stop_timer is None:
                self._stop_timer = threading.Timer(self.IDLE_STOP_SECONDS, self._stop_if_idle)
                self._stop_timer.daemon = True
                self._stop_timer.start()
        self.event_bus.emit(SharedEvent.STATE_UPDATED, self)

    def _stop_if_idle(self):
        with self._usage_lock:
            self._stop_timer = None
            if self._users or not self._started_automatically:
                return
            self._started_automatically = False
        print(f"Stopping unused virtual machine {self.name}")
        self.stop()

    def stop_if_started_automatically(self):
        """Stops machine started automatically (when app quits)."""
        with self._usage_lock:
            if self._stop_timer:
                self._stop_timer.cancel()
                self._stop_timer = None
            started_automatically = self._started_automatically
            self._started_automatically = False
        if started_automatically and self.is_running:
            self.stop()

    # Lifecycle:

    def start(self, output_handler=print, process_holder: list | None = None, automatically: bool = False) -> bool:
        """Starts machine. Machine started automatically (for operation) is stopped when it's no longer used, started
        by user keeps running."""
        with self._usage_lock:
            if not automatically:
                self._started_automatically = False # User wants it running.
        with self._lock:
            self.refresh_status()
            if self.is_running:
                return True
            if self.status == self.STATUS_MISSING:
                raise RuntimeError(f"Virtual machine {self.name} doesn't exist anymore")
            output_handler(f"Starting virtual machine {self.name}...")
            self._set_status(self.STATUS_STARTING)
            try:
                self._update_provision_script(output_handler, process_holder)
                self._update_mounts(output_handler, process_holder)
                self._prepare_swap_disk(output_handler, process_holder)
                success = run_limactl(["start", self.instance_name], output_handler, process_holder, home=self.lima_home)
            finally:
                self.refresh_status(during_transition=True)
            if success and self.is_running and self.swap_gib:
                self._activate_swap(output_handler)
            if success and self.is_running and automatically:
                with self._usage_lock:
                    self._started_automatically = True
            return success and self.is_running

    def stop(self, output_handler=print) -> bool:
        with self._usage_lock:
            self._started_automatically = False
            if self._stop_timer:
                self._stop_timer.cancel()
                self._stop_timer = None
        with self._lock:
            self._set_status(self.STATUS_STOPPING)
            try:
                success = self._stop_instance(output_handler)
            finally:
                self.refresh_status(during_transition=True)
            if self.status == self.STATUS_STOPPED:
                self._delete_workspace(lambda line: None)
                # Swapped data is not needed after machine stops, its disk takes no space until next start.
                self.delete_swap_disk(lambda line: None)
            return success

    # Time for limactl to stop machine normally, and to force stop it.
    STOP_TIMEOUT = 60
    FORCE_STOP_TIMEOUT = 30

    def _stop_instance(self, output_handler) -> bool:
        """Stops Lima instance, with force when it doesn't stop normally (eg. machine crashed and its host agent doesn't
        exit), killing its processes as last resort."""
        if run_limactl(["stop", self.instance_name], output_handler, home=self.lima_home, timeout=self.STOP_TIMEOUT):
            return True
        output_handler(f"Virtual machine {self.name} didn't stop, stopping it with force")
        # Forced stop can leave processes started by host agent running (eg. driver of crashed machine), they are
        # found before it removes pid files and killed after it.
        pids = instance_process_ids(self.instance_name, self.lima_home)
        run_limactl(["stop", "--force", self.instance_name], output_handler, home=self.lima_home, timeout=self.FORCE_STOP_TIMEOUT)
        kill_instance_processes(self.instance_name, output_handler, home=self.lima_home, pids=pids)
        instance = list_instances(self.lima_home).get(self.instance_name)
        return instance is not None and instance.get("status") == self.STATUS_STOPPED

    def ensure_running(self, output_handler=print, process_holder: list | None = None):
        """Starts machine if needed, raises when it can't be started. Setup of machine is updated once while app runs,
        when it's older than current one (machines created by previous versions)."""
        if not self.start(output_handler, process_holder, automatically=True):
            raise RuntimeError(f"Failed to start virtual machine {self.name}")
        if not getattr(self, "_setup_checked", False):
            from .lima import machine_setup_update_script
            from .rootless import MachineExecutor
            self._setup_checked = True # Before running, executor ensures machine is running too.
            if not MachineExecutor(self).run(machine_setup_update_script(), output_handler, process_holder):
                self._setup_checked = False
                raise RuntimeError(f"Failed to update setup of virtual machine {self.name}")

    # Swap:
    # Raw disk attached to machine (Lima additional disk), used only as swap, so builds needing more memory than
    # machine has (eg. large C++ projects compiled in parallel) are slower instead of killed. Disk file is sparse, it
    # takes space on this computer only when machine swaps. It's created before machine starts and deleted when it
    # stops, so this space is given back.

    @property
    def swap_disk_name(self) -> str:
        return f"{self.instance_name}-swap"

    def _prepare_swap_disk(self, output_handler, process_holder: list | None = None):
        """Creates empty swap disk and attaches it to stopped machine, or detaches and removes it when swap is
        disabled."""
        if self.swap_gib:
            run_limactl(["disk", "delete", "--force", self.swap_disk_name], lambda line: None, home=self.lima_home)
            if not run_limactl(["disk", "create", self.swap_disk_name, f"--size={self.swap_gib}GiB", "--format=raw"],
                               output_handler, process_holder, home=self.lima_home):
                raise RuntimeError(f"Failed to create swap disk of virtual machine {self.name}")
        if self._swap_disk_attached() != bool(self.swap_gib):
            expression = (f'.additionalDisks = [{{"name": "{self.swap_disk_name}", "format": false}}]' if self.swap_gib
                          else "del(.additionalDisks)")
            if not run_limactl(["edit", "--set", expression, self.instance_name], output_handler, process_holder, home=self.lima_home):
                raise RuntimeError(f"Failed to configure swap disk of virtual machine {self.name}")
        if not self.swap_gib:
            run_limactl(["disk", "delete", "--force", self.swap_disk_name], lambda line: None, home=self.lima_home)

    def _update_provision_script(self, output_handler, process_holder: list | None = None):
        """Lima runs provisioning script (stored in configuration of machine) on every boot. Machines created by
        previous versions get current script, which doesn't install packages again when machine is set up."""
        import json
        from .lima import machine_setup_script
        script = machine_setup_script()
        try:
            with open(os.path.join(self.machine_directory, "lima.yaml"), encoding="utf-8") as file:
                configuration = file.read()
        except OSError:
            return
        if all(line.strip() in configuration for line in script.splitlines() if line.strip()):
            return
        output_handler(f"Updating provisioning of virtual machine {self.name}")
        expression = f'.provision = [{{"mode": "system", "script": {json.dumps(script)}}}]'
        if not run_limactl(["edit", "--set", expression, self.instance_name], output_handler, process_holder, home=self.lima_home):
            output_handler(f"Warning: provisioning of virtual machine {self.name} was not updated")

    def _update_mounts(self, output_handler, process_holder: list | None = None):
        """Machine shares current folders of Catalyst Lab (they change when library is moved to other place)."""
        import json, re
        from .build_machine_manager import shared_paths
        paths = shared_paths()
        try:
            with open(os.path.join(self.machine_directory, "lima.yaml"), encoding="utf-8") as file:
                configuration = file.read()
        except OSError:
            return
        section = re.search(r"^mounts:\n((?:[ -].*\n)*)", configuration, re.MULTILINE)
        current = re.findall(r'location:\s*"?([^"\n]+)"?', section.group(1)) if section else []
        if sorted(path.strip() for path in current) == sorted(paths):
            return
        output_handler(f"Updating shared folders of virtual machine {self.name}")
        expression = f".mounts = {json.dumps([{'location': path, 'writable': True} for path in paths])}"
        if not run_limactl(["edit", "--set", expression, self.instance_name], output_handler, process_holder, home=self.lima_home):
            output_handler(f"Warning: shared folders of virtual machine {self.name} were not updated")

    def _swap_disk_attached(self) -> bool:
        try:
            with open(os.path.join(self.machine_directory, "lima.yaml"), encoding="utf-8") as file:
                return self.swap_disk_name in file.read()
        except OSError:
            return False

    def _activate_swap(self, output_handler):
        """Swap failure doesn't stop machine from being used, it's reported in output."""
        try:
            if not run_as_root_in_instance(self.instance_name, swap_activation_script(self.swap_gib * 1024 ** 3), output_handler, home=self.lima_home):
                output_handler(f"Warning: swap of virtual machine {self.name} is not enabled")
        except Exception as e:
            output_handler(f"Warning: failed to enable swap of virtual machine {self.name}: {e}")

    def delete_swap_disk(self, output_handler=print):
        run_limactl(["disk", "delete", "--force", self.swap_disk_name], output_handler, home=self.lima_home)

    # Settings:

    def rename(self, name: str):
        self.name = name
        Repository.BuildMachine.save()
        self.event_bus.emit(SharedEvent.STATE_UPDATED, self)

    def change_resources(self, cpus: int, memory_gib: int, workspace_gib: int, swap_gib: int, output_handler=print):
        """Changes resources of stopped machine. Processors and memory are changed in Lima instance, working space limit
        applies to next working space and swap limit to next start."""
        with self._lock:
            self.refresh_status()
            if self.status != self.STATUS_STOPPED:
                raise RuntimeError(f"Virtual machine {self.name} must be stopped to change its resources")
            if (cpus, memory_gib) != (self.cpus, self.memory_gib):
                arguments = ["edit", f"--cpus={cpus}", f"--memory={memory_gib}", self.instance_name]
                if not run_limactl(arguments, output_handler, home=self.lima_home):
                    raise RuntimeError(f"Failed to change resources of virtual machine {self.name}")
            self.cpus, self.memory_gib, self.workspace_gib, self.swap_gib = cpus, memory_gib, workspace_gib, swap_gib
        Repository.BuildMachine.save()
        self.event_bus.emit(SharedEvent.STATE_UPDATED, self)

    # Working space:
    # Ext4 image in shared folder, mounted in machine at MACHINE_DATA_DIRECTORY while operations use it (environments,
    # builds, installations). It's created for first operation and deleted when last one finishes, so data doesn't
    # stay in machine nor take space on this computer.

    @property
    def workspace_image_path(self) -> str:
        """In folder of machine (removed with it). Machines in ~/.lima, which isn't shared with them, keep it in
        machines directory."""
        from .lima import machines_directory
        directory = machines_directory()
        if self.machine_directory.startswith(directory + os.sep):
            return os.path.join(self.machine_directory, "workspace.img")
        return os.path.join(directory, self.instance_name, "workspace.img")

    @property
    def machine_directory(self) -> str:
        """Folder of machine (Lima instance directory)."""
        return os.path.join(self.lima_home, self.instance_name)

    def acquire_workspace(self, output_handler=print, process_holder: list | None = None, user: str = "Operation"):
        """Prepares working space for operation, call release_workspace with the same user when it finishes. Machine is
        used by operation until then."""
        self.begin_use(user)
        try:
            self._acquire_workspace(output_handler, process_holder)
        except Exception:
            self.end_use(user)
            raise
        self.event_bus.emit(SharedEvent.STATE_UPDATED, self)

    def _acquire_workspace(self, output_handler, process_holder):
        with self._workspace_lock:
            if self._workspace_users == 0:
                self._create_workspace(output_handler, process_holder)
            self._workspace_users += 1

    def release_workspace(self, output_handler=print, user: str = "Operation"):
        with self._workspace_lock:
            if self._workspace_users == 0:
                return
            self._workspace_users -= 1
            if self._workspace_users == 0:
                self._delete_workspace(output_handler)
        self.end_use(user)

    def _create_workspace(self, output_handler, process_holder):
        from .rootless import MachineExecutor
        image = self.workspace_image_path
        os.makedirs(os.path.dirname(image), exist_ok=True)
        output_handler(f"Preparing working space of {self.name}")
        # Left by previous session of app (eg. after crash) is replaced.
        script = f"""
set -e
sudo umount -l {MACHINE_DATA_DIRECTORY} 2>/dev/null || true
rm -f "{image}"
truncate -s {self.workspace_gib}G "{image}"
mkfs.ext4 -q -F -m 0 "{image}"
sudo mount -o loop "{image}" {MACHINE_DATA_DIRECTORY}
sudo chown "$(id -u):$(id -g)" {MACHINE_DATA_DIRECTORY}
"""
        if not MachineExecutor(self).run(script, output_handler, process_holder):
            raise RuntimeError(f"Failed to prepare working space of {self.name}")

    def _delete_workspace(self, output_handler):
        from .rootless import MachineExecutor
        image = self.workspace_image_path
        if not os.path.exists(image):
            return
        output_handler(f"Removing working space of {self.name}")
        if self.is_running:
            try:
                # Do not unlink the backing file if the guest couldn't unmount it.
                cleanup = (f'if mountpoint -q {MACHINE_DATA_DIRECTORY}; then sudo umount -l {MACHINE_DATA_DIRECTORY}; fi; '
                           f'rm -f "{image}"')
                if not MachineExecutor(self).run(cleanup, output_handler):
                    output_handler(f"Failed to remove working space of {self.name}: guest unmount failed")
                    return
            except Exception as e:
                output_handler(f"Failed to unmount working space: {e}")
                return
        try:
            if os.path.exists(image):
                os.remove(image)
        except OSError as e:
            output_handler(f"Failed to remove working space of {self.name}: {e}")

    def cleanup_workspace(self, output_handler=print):
        """Removes a workspace left behind by a crashed or previously closed app."""
        if self._workspace_users:
            return
        self._delete_workspace(output_handler)

def build_machine_for_id(machine_id: uuid.UUID | None) -> BuildMachine | None:
    if machine_id is None:
        return None
    return next((machine for machine in Repository.BuildMachine.value if machine.id == machine_id), None)
