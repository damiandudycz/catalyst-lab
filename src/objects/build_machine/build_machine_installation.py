from __future__ import annotations
import os, tempfile
from .multistage_process import MultiStageProcess, MultiStageProcessStage, MultiStageProcessStageState, MultiStageProcessState
from .build_machine import BuildMachine
from .build_machine_manager import BuildMachineManager, shared_paths
from .lima import lima_unavailable_reason, machine_configuration, run_limactl
from .rootless import MachineExecutor

# ------------------------------------------------------------------------------
# Creating build machine.
# ------------------------------------------------------------------------------

class BuildMachineInstallation(MultiStageProcess):
    """Creates Alpine Linux virtual machine with Lima, prepared for building without root privileges."""

    def __init__(self, name: str, cpus: int, memory_gib: int, workspace_gib: int, swap_gib: int = BuildMachine.DEFAULT_SWAP_GIB):
        self.machine = BuildMachine(name=name, cpus=cpus, memory_gib=memory_gib, workspace_gib=workspace_gib, swap_gib=swap_gib)
        super().__init__(title="Virtual machine creation")

    def setup_stages(self):
        self.stages.append(BuildMachineStepCheckLima(multistage_process=self))
        self.stages.append(BuildMachineStepCreate(multistage_process=self))
        self.stages.append(BuildMachineStepStart(multistage_process=self))
        self.stages.append(BuildMachineStepVerify(multistage_process=self))

    def complete_process(self, success: bool):
        if success:
            BuildMachineManager.shared().add_machine(self.machine)

    def name(self) -> str:
        return self.machine.name

class BuildMachineStep(MultiStageProcessStage):
    def start(self):
        self.processes = [] # Lima processes, terminated when canceled.
        super().start()
    def cancel(self):
        super().cancel()
        for process in getattr(self, "processes", []):
            if hasattr(process, "terminate"):
                process.terminate()

class BuildMachineStepCheckLima(BuildMachineStep):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Check virtual machine tools", description="Checks that Lima is installed", multistage_process=multistage_process)
    def start(self):
        super().start()
        try:
            if reason := lima_unavailable_reason():
                raise RuntimeError(reason)
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during '{self.name}': {e}")
            self.complete(MultiStageProcessStageState.FAILED)

class BuildMachineStepCreate(BuildMachineStep):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Create virtual machine", description="Downloads Alpine Linux image and creates machine", multistage_process=multistage_process)
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
                    raise RuntimeError("Failed to create virtual machine")
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

class BuildMachineStepStart(BuildMachineStep):
    # Machine is used by creation until it finishes, so it's not stopped (when idle) between steps and booted again.
    USER = "Machine creation"
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Start virtual machine", description="Boots machine and installs required packages", multistage_process=multistage_process)
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

class BuildMachineStepVerify(BuildMachineStep):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(name="Verify machine", description="Checks that toolsets can run in machine without root", multistage_process=multistage_process)
    def start(self):
        super().start()
        try:
            executor = MachineExecutor(self.multistage_process.machine)
            if reason := executor.toolset_unsupported_reason(output_handler=self.log):
                raise RuntimeError(reason)
            if not executor.run_in_namespace("id; touch /tmp/owner-test && chown 250:250 /tmp/owner-test && echo 'Mapped ids work'", self.log, self.processes):
                raise RuntimeError("Commands can't run in user namespace of machine")
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during '{self.name}': {e}")
            self.complete(MultiStageProcessStageState.FAILED)
