from .multistage_process import MultiStageProcessStage, MultiStageProcessStageState


class DeployStep(MultiStageProcessStage):
    """Runs one deployment step and records its output."""

    def log(self, line: str):
        super().log(line)
        self.multistage_process.write_log(line)

    def start(self):
        self.processes = []
        super().start()
        self.multistage_process.write_log(f"=== {self.name}")
        try:
            self.run()
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as error:
            print(f"Error during '{self.name}': {error}")
            self.complete(MultiStageProcessStageState.FAILED)

    def run(self):
        raise NotImplementedError

    def remote(self, script: str, error: str):
        if not self.multistage_process.connection.run(script, self.log, self.processes):
            raise RuntimeError(error)

    def cancel(self):
        super().cancel()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
