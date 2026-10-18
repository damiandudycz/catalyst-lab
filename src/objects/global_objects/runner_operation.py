"""Shared lifecycle stages used by long-running operations on any runner type."""
from __future__ import annotations

import uuid

from .multistage_process import MultiStageProcessStage, MultiStageProcessStageState


class PrepareRunnerStage(MultiStageProcessStage):
    def __init__(self, process):
        super().__init__(name="Prepare Runner", description="Prepares the selected execution runner", multistage_process=process)

    def start(self):
        super().start()
        process = self.multistage_process
        try:
            should_prepare = getattr(process, "should_prepare_runner", None)
            if callable(should_prepare) and not should_prepare(self.log):
                self.log("Runner is not needed for this operation")
                self.complete(MultiStageProcessStageState.COMPLETED)
                return
            runner = getattr(process, "runner", None)
            if runner is None:
                raise RuntimeError("No runner is configured for this operation")
            process.runner_operation_user = f"{process.title} {uuid.uuid4().hex}"
            prepare_arguments = (self.log, getattr(self, "namespace_processes", None), process.runner_operation_user)
            if getattr(getattr(runner, "runner_type", None), "value", None) == "host":
                runner.prepare(*prepare_arguments, exclusive=getattr(process, "runner_exclusive", False))
            else:
                runner.prepare(*prepare_arguments)
            process.runner_prepared = True
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as error:
            self.log(f"Runner preparation failed: {error}")
            self.complete(MultiStageProcessStageState.FAILED)

    def cleanup(self) -> bool:
        prepared = getattr(self.multistage_process, "runner_prepared", False)
        if not super().cleanup():
            return False
        if prepared:
            self.multistage_process.runner.release(self.multistage_process.runner_operation_user)
            self.multistage_process.runner_prepared = False
        return True


class StopRunnerStage(MultiStageProcessStage):
    def __init__(self, process):
        super().__init__(name="Stop Runner", description="Releases the selected execution runner", multistage_process=process)

    def start(self):
        super().start()
        process = self.multistage_process
        try:
            if getattr(process, "runner_prepared", False):
                self.log(f"Stop Runner: {process.runner.name}")
                process.runner.release(process.runner_operation_user)
                process.runner_prepared = False
            else:
                self.log("Runner was not started for this operation")
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as error:
            self.log(f"Runner release failed: {error}")
            self.complete(MultiStageProcessStageState.FAILED)
