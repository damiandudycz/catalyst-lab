import os
from .multistage_process import MultiStageProcessStage, MultiStageProcessStageState
from .ssh_connection import quote


def check_tools_script(tools: list[str], hint: str | None = None) -> str:
    """Shell check for required deployment tools, with an optional package hint."""
    tools = " ".join(quote(tool) for tool in tools)
    message = "Missing tools:$missing" + (f" ({hint})" if hint else "")
    return f'''missing=""
for tool in {tools}; do command -v "$tool" > /dev/null || missing="$missing $tool"; done
[ -z "$missing" ] || {{ echo {quote(message)}; exit 1; }}
'''


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

    def copy_file(self, source: str, destination: str):
        """Copy to a temporary sibling, reporting progress and honoring cancellation."""
        partial = destination + ".part"
        size, copied = os.path.getsize(source) or 1, 0
        try:
            with open(source, "rb") as input_file, open(partial, "wb") as output_file:
                while chunk := input_file.read(8 * 1024 * 1024):
                    if self._cancel_event.is_set():
                        raise RuntimeError("Canceled")
                    output_file.write(chunk)
                    copied += len(chunk)
                    self._update_progress(copied / size * 0.99)
            os.replace(partial, destination)
        except BaseException:
            try:
                os.remove(partial)
            except OSError:
                pass
            raise

    def cancel(self):
        super().cancel()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
