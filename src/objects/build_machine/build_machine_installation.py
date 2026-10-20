from __future__ import annotations
import os, tempfile
from .multistage_process import MultiStageProcess, MultiStageProcessStage, MultiStageProcessStageState, MultiStageProcessState
from .build_machine import VMRunner
from .build_machine_manager import VMRunnerManager, shared_paths
from .lima import lima_unavailable_reason, machine_configuration, run_limactl
from .rootless import MachineExecutor

# ------------------------------------------------------------------------------
# Creating a VM runner.
# ------------------------------------------------------------------------------

class VMRunnerInstallation(MultiStageProcess):
    """Creates an Alpine Linux virtual machine for root execution inside the guest."""

    def __init__(self, name: str, cpus: int, memory_gib: int, workspace_gib: int,
                 swap_gib: int = VMRunner.DEFAULT_SWAP_GIB, set_as_default: bool = True):
        self.machine = VMRunner(name=name, cpus=cpus, memory_gib=memory_gib, workspace_gib=workspace_gib, swap_gib=swap_gib)
        self.set_as_default = set_as_default
        super().__init__(title="VM runner creation")

    def setup_stages(self):
        self.stages.append(VMRunnerStepCheckLima(multistage_process=self))
        self.stages.append(VMRunnerStepCreate(multistage_process=self))
        self.stages.append(VMRunnerStepStart(multistage_process=self))
        self.stages.append(VMRunnerStepVerify(multistage_process=self))

    def complete_process(self, success: bool):
        if success:
            VMRunnerManager.shared().add_runner(self.machine)
            if self.set_as_default:
                from .repository import Repository
                Repository.Settings.value.default_runner_id = str(self.machine.id)

    def name(self) -> str:
        return self.machine.name

class VMRunnerStep(MultiStageProcessStage):
    def start(self):
        self.processes = [] # Lima processes, terminated when canceled.
        super().start()
    def cancel(self):
        super().cancel()
        for process in getattr(self, "processes", []):
            if hasattr(process, "terminate"):
                process.terminate()

class VMRunnerStepCheckLima(VMRunnerStep):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Check VM runner tools", description="Checks that Lima is installed", multistage_process=multistage_process)
    def start(self):
        super().start()
        try:
            if reason := lima_unavailable_reason():
                raise RuntimeError(reason)
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during '{self.name}': {e}")
            self.complete(MultiStageProcessStageState.FAILED)

class VMRunnerStepCreate(VMRunnerStep):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Create VM runner", description="Downloads Alpine Linux image and creates a runner", multistage_process=multistage_process)
        self.created = False
    def start(self):
        super().start()
        try:
            machine = self.multistage_process.machine
            paths = shared_paths()
            self.log(f"Shared folders: {', '.join(paths)}")
            configuration = machine_configuration(cpus=machine.cpus, memory_gib=machine.memory_gib, shared_paths=paths)
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as file:
                file.write(configuration)
                configuration_path = file.name
            try:
                if not run_limactl(["create", f"--name={machine.instance_name}", configuration_path], self.log, self.processes, home=machine.lima_home):
                    raise RuntimeError("Failed to create VM runner")
            finally:
                os.remove(configuration_path)
            self.created = True
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during '{self.name}': {e}")
            self.complete(MultiStageProcessStageState.FAILED)
    def cleanup(self) -> bool:
        if not super().cleanup():
            return False
        # Machine is removed when creation didn't finish.
        if self.created and self.multistage_process.status != MultiStageProcessState.COMPLETED:
            run_limactl(["delete", "--force", self.multistage_process.machine.instance_name], print, home=self.multistage_process.machine.lima_home)
            self.multistage_process.machine.delete_swap_disk()
        return True

class VMRunnerStepStart(VMRunnerStep):
    # Machine is used by creation until it finishes, so it's not stopped (when idle) between steps and booted again.
    USER = "Machine creation"
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Start VM runner", description="Boots the runner and installs required packages", multistage_process=multistage_process)
        self.used = False
    def start(self):
        super().start()
        try:
            self.multistage_process.machine.begin_use(self.USER)
            self.used = True
            self.multistage_process.machine.ensure_running(self.log, self.processes)
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during '{self.name}': {e}")
            self.complete(MultiStageProcessStageState.FAILED)
    def cleanup(self) -> bool:
        if not super().cleanup():
            return False
        if self.used:
            self.used = False
            machine = self.multistage_process.machine
            machine.end_use(self.USER)
            # Machine of failed creation is stopped now, before it's removed.
            if self.multistage_process.status != MultiStageProcessState.COMPLETED:
                machine.stop_if_started_automatically()
        return True

class VMRunnerStepVerify(VMRunnerStep):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Verify runner", description="Checks that toolset commands can run as root inside the guest", multistage_process=multistage_process)
    def start(self):
        super().start()
        try:
            executor = MachineExecutor(self.multistage_process.machine)
            if reason := executor.toolset_unsupported_reason(output_handler=self.log):
                raise RuntimeError(reason)
            if not executor.run("test \"$(id -u)\" -eq 0 && echo 'Guest root execution works'", self.log, self.processes):
                raise RuntimeError("Commands can't run as root inside the VM runner")
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during '{self.name}': {e}")
            self.complete(MultiStageProcessStageState.FAILED)
