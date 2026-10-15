from __future__ import annotations
import socket, threading
from .root_function import root_function
from .root_helper_client import RootHelperClient
from .root_helper_server import ServerResponseStatusCode

# ------------------------------------------------------------------------------
# Disks of this computer as deployment target (SD cards, external disks), Linux only. Used like SSHConnection by
# deployment: scripts run as root on this computer through root helper, which stays authorized while connection is
# open. Installed system boots on other machine.

# Output lines of deploy_run_script reporting progress of sent input file.
_PROGRESS_MARKER = "@@catalystlab-deploy-progress "

class LocalDiskConnection:
    is_local = True

    def __init__(self):
        self.host = socket.gethostname() or "this computer"
        self.connected = False
        self._authorization_keeper = None

    # Connection:

    def connect(self, password: str | None = None) -> str | None:
        """Asks for root access. Returns error message, or None when authorized."""
        authorized = threading.Event()
        keepers = []
        def callback(authorization_keeper):
            # Keeper is released when callback returns, connection retains it until closed.
            if authorization_keeper:
                authorization_keeper.retain()
            keepers.append(authorization_keeper)
            authorized.set()
        RootHelperClient.shared().authorize_and_run(name="Deploy", callback=callback)
        authorized.wait()
        self._authorization_keeper = keepers[0]
        if self._authorization_keeper is None:
            return "Root access is needed to install on disks of this computer"
        self.connected = True
        return None

    def close(self):
        keeper, self._authorization_keeper = self._authorization_keeper, None
        self.connected = False
        if keeper:
            keeper.release()

    def is_alive(self) -> bool:
        return self.connected and RootHelperClient.shared().ensure_server_ready()

    # Commands:

    def run(self, script: str, output_handler=print, process_holder: list | None = None) -> bool:
        """Runs bash script as root, output goes to handler line by line."""
        return self._call(script, None, output_handler, None, process_holder)

    def output(self, script: str) -> str:
        """Output of script, raises when it fails."""
        lines = []
        if not self.run(script, lines.append):
            raise RuntimeError(lines[-1] if lines else "Command failed")
        return "\n".join(lines)

    def stream_file(self, path: str, command: str, output_handler=print, progress_handler=None,
                    process_holder: list | None = None) -> bool:
        """Sends file to stdin of command (eg. tar extracting it). Progress handler gets fraction of sent data."""
        return self._call(command, path, output_handler, progress_handler, process_holder)

    def _call(self, script: str, input_path: str | None, output_handler, progress_handler, process_holder) -> bool:
        call = _RootCall()
        if process_holder is not None:
            process_holder.append(call)
        def handle_line(line: str):
            if line.startswith(_PROGRESS_MARKER):
                if progress_handler:
                    try:
                        progress_handler(float(line[len(_PROGRESS_MARKER):]))
                    except ValueError:
                        pass
            else:
                output_handler(line)
        try:
            call.server_call = deploy_run_script._async_raw(handler=handle_line, completion_handler=call.finish,
                                                            script=script, input_path=input_path)
        except Exception as e:
            output_handler(f"Failed to run command as root: {e}")
            call.finish(None)
            return False
        call.finished.wait()
        response = call.response
        if response is None or response.code != ServerResponseStatusCode.OK:
            if response is not None and response.code != ServerResponseStatusCode.JOB_WAS_TERMINATED:
                output_handler(f"Command failed: {response.response or response.code.name}")
            return False
        return response.response == 0

class _RootCall:
    """Running root helper call, used like process (poll, terminate) by deploy steps."""

    def __init__(self):
        self.server_call = None
        self.response = None
        self.finished = threading.Event()

    def finish(self, response):
        self.response = response
        self.finished.set()

    def poll(self) -> int | None:
        return 0 if self.finished.is_set() else None

    def terminate(self):
        if self.server_call and not self.finished.is_set():
            self.server_call.cancel()

@root_function
def deploy_run_script(script: str, input_path: str | None = None) -> int:
    """Runs deploy script with bash, returns its exit code. Input file is sent to stdin of script, with progress lines
    in output. Script is stopped with all its processes when call is cancelled."""
    import os, signal, subprocess, sys, time
    marker = "@@catalystlab-deploy-progress " # _PROGRESS_MARKER, root functions run without module globals.
    sys.stdout.flush()
    process = subprocess.Popen(["bash", "-c", script], stdin=subprocess.PIPE if input_path else subprocess.DEVNULL,
                               stderr=subprocess.STDOUT, start_new_session=True)
    def stop(signum, frame):
        # Root helper gives call 3 seconds to end after SIGTERM, then kills it.
        try:
            os.killpg(process.pid, signal.SIGTERM)
            deadline = time.monotonic() + 2
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError): # Group already ended.
            pass
        os._exit(143)
    signal.signal(signal.SIGTERM, stop)
    if input_path:
        size = os.path.getsize(input_path) or 1
        sent = 0
        reported = -1
        try:
            with open(input_path, "rb") as file:
                while chunk := file.read(1024 * 1024):
                    process.stdin.write(chunk)
                    sent += len(chunk)
                    if (percent := sent * 100 // size) != reported:
                        reported = percent
                        print(f"{marker}{sent / size}", flush=True)
            process.stdin.close()
        except (BrokenPipeError, ValueError):
            pass # Script ended (failed), its exit code is reported.
    return process.wait()
