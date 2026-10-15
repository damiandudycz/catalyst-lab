#!/usr/bin/env python3
from __future__ import annotations
import os, re, socket, subprocess, uuid, time
import threading, inspect, select, shutil
from enum import Enum
from typing import Any, Callable
from gi.repository import Gio
from dataclasses import dataclass, field
from .runtime_env import RuntimeEnv
from .settings import *
from .root_helper_server import ServerCommand, ServerFunction
from .root_helper_server import ServerResponse, ServerResponseStatusCode
from .root_helper_server import RootHelperServer, StreamPipe, StreamPipeEvent, WatchDog
from .root_function import ROOT_FUNCTION_REGISTRY

class RootHelperClient:

    _instance: RootHelperClient | None = None # Singleton shared instance.
    use_server_watchdog = False # Enable for release. Might disable for debugging.

    # --------------------------------------------------------------------------
    # Lifecycle:

    def __init__(self):
        self.event_bus = EventBus[RootHelperClientEvents]()
        self.socket_path = RootHelperServer.get_socket_path(os.getuid())
        self.main_process = None
        self.running_actions: list[ServerCall] = []
        self.token = None
        self.keep_unlocked = Repository.Settings.value.keep_root_unlocked
        self.authorization_keepers: list[AuthorizationKeeper] = []
        self.server_watchdog = WatchDog(lambda: self.ping_server())
        self.set_request_status_lock = threading.RLock()
        Repository.Settings.value.event_bus.subscribe(
            SettingsEvents.KEEP_ROOT_UNLOCKED_CHANGED,
            self.keep_root_unlocked_changed
        )

    @classmethod
    def shared(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    is_server_process_running = property(
        fget=lambda self: getattr(self, "_is_server_process_running", False),
        fset=lambda self, value: (
            setattr(self, "_is_server_process_running", value),
            self.event_bus.emit(RootHelperClientEvents.CHANGE_ROOT_ACCESS, value),
            (not value and self.server_watchdog.stop() or None)
        )[0]
    )

    def server_handshake_established(self) -> bool:
        return self.token is not None

    # --------------------------------------------------------------------------
    # Server lifecycle management:

    def start_root_helper(self) -> bool:
        """Start the root helper process."""
        if self.is_server_process_running:
            print("Root helper is already running.")
            return True
        with self.set_request_status_lock:
            if self.running_actions:
                print("Error: Some actions are not finished yet.")
                return False
        try:
            if os.path.exists(self.socket_path):
                os.remove(self.socket_path)
            self.is_server_process_running = True
            token = str(uuid.uuid4())
            self.token = None

            helper_host_path = self.extract_root_helper_to_run_user(os.getuid())
            xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
            if RuntimeEnv.current() == RuntimeEnv.FLATPAK:
                cmd_prefix = ["flatpak-spawn", "--host"]
            else:
                cmd_prefix = []
            cmd_authorize = ["pkexec"]
            exec_call = cmd_prefix + cmd_authorize + [helper_host_path]

            # Start pkexec and pass token via stdin
            # Note: This is flatpak-spawn process, not server process itself.
            # Note: DO NOT ADD stdout/stderr redirections to this. It can cause freezing after long output was produced.
            self.main_process = subprocess.Popen(exec_call, stdin=subprocess.PIPE)
            time.sleep(0.5) # This helps prevent an issue with server not being ready to read from stdin at start. Probably not the best solution, might find better one.

            # Waits until main_process finishes, to see if it was closed with error.
            def monitor_error_codes():
                if self.main_process.wait() != 0 and self.is_server_process_running:
                    print("[Server process]! Authorization failed or cancelled.")
                    self.is_server_process_running = False
            threading.Thread(target=monitor_error_codes, daemon=True).start()

            # Send token and runtime dir
            # TODO: This method of passing sometimes fails, leaving server waiting for that data.
            self.main_process.stdin.write(token.encode() + b' ')
            self.main_process.stdin.write(xdg_runtime_dir.encode() + b'\n')
            self.main_process.stdin.flush()

            # Wait for the server initialization to return result.
            if self.initialize_server_connectivity(token=token):
                self.token = token
                return True
            else:
                raise RuntimeError("Failed to initialize connection.")
        except Exception as e:
            print(f"FAILED TO START SERVER: {e}")
            self.is_server_process_running = False
            self.main_process = None
            return False

    def stop_root_helper(self, instant: bool = False):
        """Stop the root helper process."""
        if not self.is_server_process_running:
            print(f"Server is already stopped")
            return
        try:
            token = self.token
            self.token = None
            self.server_watchdog.stop()
            if instant:
                self.clean_unfinished_jobs()
            else:
                def complete_exit(response: ServerResponse):
                    # Mark exit call as completed earlier.
                    exit_call = next((action for action in self.running_actions if action.request == ServerCommand.EXIT), None)
                    if exit_call:
                        self.set_request_status(exit_call, False)
                    self.clean_unfinished_jobs()
                self.send_request(ServerCommand.EXIT, asynchronous=True, completion_handler=complete_exit, token=token)
        except Exception as e:
            print("[Server process]: Warning: Failed to send EXIT command. Some process might be left working orphined.")
            self.clean_unfinished_jobs()
        finally:
            self.is_server_process_running = False
            self.main_process = None
            print("[Server process]: Closed.")

    def clean_unfinished_jobs(self):
        print("clean_unfinished_jobs")
        """Marks all jobs as finished, even if server didn't yet closed them."""
        """If job is still on the list at this point it means it's orphined - we don't know if it's still running."""
        with self.set_request_status_lock:
            for call in self.running_actions[:]:
                print(f"[Server process]: Warning: Call {call} was left orphined.")
                self.set_request_status(call, False)

    def ping_server(self):
        try:
            self.send_request(ServerCommand.PING)
        finally:
            pass # Server disconnection is handled in send_request.

    def extract_root_helper_to_run_user(self, uid: int) -> str:
        """Extracts root helper server code and appends root functions."""
        # Runtime directory where the generated server code will be placed.
        runtime_dir = RootHelperServer.get_runtime_dir(uid)
        output_path = os.path.join(runtime_dir, "root-helper-server.py")
        # Ensure the directory exists
        os.makedirs(runtime_dir, exist_ok=True)
        # If the file already exists, remove it
        if os.path.exists(output_path):
            os.remove(output_path)

        # Load the embedded server code from resources
        data = Gio.resources_lookup_data('/com/damiandudycz/CatalystLab/objects/root_helper/root_helper_server.py', Gio.ResourceLookupFlags.NONE)
        server_code = data.get_data().decode()
        # Collect the root functions (dynamically registered)
        injected_functions = self.collect_root_function_sources()
        # Combine the server code with the dynamically injected functions
        full_code = server_code + "\n\n" + injected_functions
        # Add the `if __name__ == "__main__":` block to run the server
        # This needs to be added bellow dynamic functions.
        full_code += """\n\nif __name__ == "__main__":\n    __init_server__()"""

        # Write the full code to the output file
        with open(output_path, "w") as f:
            f.write(full_code)
        # Make the script executable
        os.chmod(output_path, 0o700)

        # Provide bwrap in runtime dir, where toolset commands expect it.
        # Use host bwrap if it supports options used by toolsets, otherwise use bwrap bundled with flatpak.
        # Root helper runs in host, so bundled bwrap needs to be copied out of flatpak.
        bwrap_output_path = os.path.join(runtime_dir, "bwrap")
        if os.path.lexists(bwrap_output_path):
            os.remove(bwrap_output_path)
        host_bwrap_path, host_bwrap_version = self.find_host_bwrap()
        if host_bwrap_path and host_bwrap_version >= RootHelperClient.MIN_BWRAP_VERSION:
            os.symlink(host_bwrap_path, bwrap_output_path)
        elif RuntimeEnv.current() == RuntimeEnv.FLATPAK:
            shutil.copy("/app/bin/bwrap", bwrap_output_path)
        else:
            min_version = ".".join(map(str, RootHelperClient.MIN_BWRAP_VERSION))
            found = f"found {'.'.join(map(str, host_bwrap_version))}" if host_bwrap_path else "not found"
            print(f"Warning: bwrap >= {min_version} is required by toolset environments ({found}).")

        return output_path

    # Toolset bindings use --overlay-src and --overlay, which were added in bubblewrap 0.11.0.
    MIN_BWRAP_VERSION = (0, 11, 0)

    def find_host_bwrap(self) -> tuple[str | None, tuple[int, ...]]:
        """Returns path and version of bwrap installed in host system, (None, ()) if not available."""
        cmd_prefix = ["flatpak-spawn", "--host"] if RuntimeEnv.current() == RuntimeEnv.FLATPAK else []
        try:
            path = subprocess.run(
                cmd_prefix + ["sh", "-c", "command -v bwrap"],
                capture_output=True, text=True, timeout=10
            ).stdout.strip()
            if not path:
                return None, ()
            version_output = subprocess.run(
                cmd_prefix + [path, "--version"],
                capture_output=True, text=True, timeout=10
            ).stdout # Format: "bubblewrap 0.11.2"
            version = tuple(int(number) for number in re.findall(r"\d+", version_output)[:3])
            return (path, version) if version else (None, ())
        except Exception as e:
            print(f"Failed to check host bwrap: {e}")
            return None, ()

    def collect_root_function_sources(self) -> str:
        """Returns all registered root function sources as a single"""
        """Python string, with @root_function decorators removed."""
        if not ROOT_FUNCTION_REGISTRY:
            return ""
        sources = []
        for func in ROOT_FUNCTION_REGISTRY.values():
            try:
                source = inspect.getsource(func)
                sources.append(source.strip())
            except OSError:
                print(f"Warning: could not get source for function {func.__name__}")
        return "\n\n# ---- Injected root functions ----\n\n" + "\n\n".join(sources)

    def initialize_server_connectivity(self, token: str, timeout: int | None = None) -> bool:
        """Wait for the server to send an 'OK' message indicating it's ready."""
        start_time = time.time()
        while timeout is None or time.time() - start_time < timeout:
            # If server was never started or killed before initialization finished.
            if not self.is_server_process_running:
                break
            try:
                response = self.send_request(ServerCommand.HANDSHAKE, token=token)
                if RootHelperClient.use_server_watchdog:
                    self.server_watchdog.start()
                return response.code == ServerResponseStatusCode.OK
            except Exception as e:
                if e == ServerCallError.SERVER_NOT_RESPONDING:
                    time.sleep(1)
                    continue
                else:
                    print(f"Unexpected error while waiting for server: {e}")
                    break
        return False

    def ensure_server_ready(self, allow_auto_start=False) -> bool:
        """Ensure the root helper server is running and the socket is"""
        """available. If allowed automatically start the server (This should"""
        """be used only by handshake request)."""
        if self.is_server_process_running:
            return os.path.exists(self.socket_path)
        elif allow_auto_start:
            print("[Root helper is not running, attempting to start it.]")
            return self.start_root_helper()
        else:
            return False

    def keep_authorization(self, name: str) -> AuthorizationKeeper:
        """Register AuthorizationKeeper to keep the root access from automatically disabling after command finishes."""
        """If server is not currently authorized, throws an error."""
        if not self.ensure_server_ready():
            raise RuntimeError("Failed to keep authorization. Server not authorized")
        keeper = AuthorizationKeeper(name=name)
        self.authorization_keepers.append(keeper)
        keeper.event_bus.subscribe(
            AuthorizationKeeperEvent.RETAIN_COUNTER_REACHED_0,
            self.authorization_keeper_released
        )
        return keeper

    def authorization_keeper_released(self, authorization_keeper: AuthorizationKeeper):
        self.authorization_keepers.remove(authorization_keeper)
        if not self.authorization_keepers and not authorization_keeper.ignore_released:
            if not self.running_actions and not self.keep_unlocked and self.server_handshake_established() and self.is_server_process_running:
                self.stop_root_helper()

    def authorize_and_run(self, name: str = "", callback: Callable[[AuthorizationKeeper | None], None] | None = None):
        def background_task():
            ensure_server_ready_result = self.ensure_server_ready(allow_auto_start=True)
            # Run immediately in background — Timer is unnecessary here
            def complete():
                if ensure_server_ready_result:
                    keeper = self.keep_authorization(name=name)
                    if callback:
                        callback(keeper)
                    keeper.release()
                else:
                    if callback:
                        callback(None)
            threading.Thread(target=complete, daemon=True).start()
        threading.Thread(target=background_task, daemon=True).start()

    # --------------------------------------------------------------------------
    # Handling requests to server / root functions:

    def send_request(
        self,
        request: ServerCommand | ServerFunction,
        command_value: str | None = None,
        handler: Callable[[str],None] | None = None,
        asynchronous: bool = False,
        raw: bool = False,
        completion_handler: Callable[[ServerResponse | Any],None] | None = None,
        token: str | None = None
    ) -> ServerResponse | ServerCall: # For async always returns ServerCall or throws.
        """Send a command to the root helper server."""
        if request != ServerCommand.EXIT and not self.ensure_server_ready():
            print("Server not responding")
            if request != ServerCommand.HANDSHAKE and self.is_server_process_running:
                print("[Server process] Server communication broke.")
                self.stop_root_helper(instant=True) # This should never happen.
            raise ServerCallError.SERVER_NOT_RESPONDING
        if request == ServerCommand.EXIT and not self.ensure_server_ready():
            print("Server not responding")
            raise ServerCallError.SERVER_NOT_RESPONDING

        # Prepare message and type
        if isinstance(request, ServerFunction):
            message, request_type = request.to_json(), "function"
        elif isinstance(request, ServerCommand):
            request_type = "command"
            message = request.value if command_value is None else request.value + " " + command_value
        else:
            raise TypeError("command must be either a ServerCommand or ServerFunction instance")

        def worker(call: ServerCall) -> ServerResponse:
            args = request.args if hasattr(request, "args") else ""
            kwargs = request.kwargs if hasattr(request, "kwargs") else ""
            all_args: list[str] = []
            if args:
                all_args.append(f"{args}")
            if kwargs:
                all_args.append(f"{kwargs}")
            if command_value:
                all_args.append(f"{command_value}")
            conn = None
            response_string: str = None
            print(f">>> [{request.function_name} {', '.join(all_args)}]")

            try:
                used_token = token if token is not None else self.token
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.connect(self.socket_path)
                conn.sendall(f"{used_token} {call.call_id} {request_type} {message} {used_token}".encode())

                current_message_type: StreamPipe | None = None
                current_chars_left: int | None = None
                current_buffer = ""

                def handle_message(message_type: StreamPipe, content: str):
                    nonlocal response_string
                    match message_type:
                        case StreamPipe.RETURN:
                            response_string = content
                        case StreamPipe.STDIN:
                            call.output_append(content)
                            if handler:
                                handler(content)
                        case StreamPipe.STDOUT:
                            call.output_append(content)
                            if handler:
                                handler(content)
                        case StreamPipe.STDERR:
                            call.output_append(content)
                            if handler:
                                handler(content)
                        case StreamPipe.EVENTS:
                            try:
                                event = StreamPipeEvent(int(content))
                                match event:
                                    case StreamPipeEvent.CALL_WILL_TERMINATE:
                                        call.mark_terminated()
                                    case _:
                                        print(f"[Server process]: Warning: Received unsupported event: {event}")
                            except Exception:
                                print(f"[Server process]: Warning: Failed to process event: {content}")

                header_buffer = ""
                current_message_type = None
                current_chars_left = None
                current_buffer = ""

                def process_fragment(fragment: str):
                    nonlocal header_buffer, current_message_type, current_chars_left, current_buffer
                    while fragment:
                        if current_message_type is None:
                            header_buffer += fragment
                            if ':' not in header_buffer:
                                return  # Not enough to parse yet
                            parts = header_buffer.split(":", 2)
                            if len(parts) < 3:
                                return  # Incomplete header, wait for more data
                            type_str, length_str, rest = parts
                            try:
                                current_message_type = StreamPipe(int(type_str))
                                current_chars_left = int(length_str)
                                current_buffer = ""
                                fragment = rest
                                header_buffer = ""
                            except Exception as e:
                                print(f"[Server]: Malformed header: {header_buffer}")
                                header_buffer = ""
                                raise e
                        else:
                            append_fragment = fragment[:current_chars_left]
                            remaining_fragment = fragment[current_chars_left:]
                            current_buffer += append_fragment
                            current_chars_left -= len(append_fragment)
                            if current_chars_left == 0:
                                handle_message(current_message_type, current_buffer)
                                current_message_type = None
                                current_chars_left = None
                                current_buffer = ""
                            fragment = remaining_fragment

                timeout = request.timeout()
                if timeout:
                    conn.settimeout(timeout)

                while (chunk := conn.recv(4096)):
                    fragment = chunk.decode()
                    process_fragment(fragment)

                server_response = ServerResponse.from_json(response_string)

            except Exception as e:
                print(f"Exception: {e}")
                server_response = ServerResponse(code=ServerResponseStatusCode.COMMAND_EXECUTION_FAILED)
                self.stop_root_helper(instant=True)

            finally:
                def send_ack():
                    try:
                        if conn:
                            readable, writable, errored = select.select([], [conn], [], 5)
                            if writable:
                                conn.sendall(b"ACK")
                                conn.shutdown(socket.SHUT_WR)
                            else:
                                print("[Server process]: Warning: Failed to send ACK back. Socket not ready for writing.")
                                raise RuntimeError("Failed to send ACK in time. Socket not ready to write.")
                    except Exception as e:
                        print(f"[Server process]: Warning: Exception while sending ACK: {e}")

                print(f"<<< [{request.function_name} {server_response.code.name}] {server_response.response}")
                if server_response.code != ServerResponseStatusCode.OK:
                    # Make failure reason visible to output observers.
                    call.output_append(f"[{server_response.code.name}] {server_response.response or ''}".rstrip())
                if completion_handler:
                    result = server_response if raw else server_response.response
                    completion_handler(result)
                if request.show_in_running_tasks:
                    if request != ServerCommand.EXIT: # Exit call is completed earlier in completion_handler
                        self.set_request_status(call, False)

                send_ack()

                if conn:
                    try:
                        conn.close()
                    except Exception as e:
                        print(f"[Server process]: Warning: Exception closing socket: {e}")

            return server_response

        if asynchronous:
            async_call = ServerCall(request=request, client=self)
            thread = threading.Thread(target=worker, args=(async_call,), daemon=True)
            async_call.thread = thread
            if request.show_in_running_tasks:
                self.set_request_status(async_call, True)
            thread.start()
            return async_call
        else:
            sync_call = ServerCall(request=request, client=self)
            if request.show_in_running_tasks:
                self.set_request_status(sync_call, True)
            return worker(call=sync_call)

    def call_root_function(
        self,
        func_name: str,
        *args,
        handler: Callable[[str],None] | None = None,
        asynchronous: bool = False,
        raw: bool = False,
        completion_handler: Callable[[ServerResponse | Any],None] | None = None,
        **kwargs
    ) -> Any | ServerResponse | ServerCall:
        """Calls function registered in ROOT_FUNCTION_REGISTRY with @root_function by its name on the server."""
        function = ServerFunction(func_name, *args, **kwargs)

        result = self.send_request(
            function,
            handler=handler,
            asynchronous=asynchronous,
            raw=raw,
            completion_handler=completion_handler
        )
        if asynchronous or raw: # Returns ServerCall for async or whole structure directly for sync_raw
            return result
        if result.code == ServerResponseStatusCode.OK:
            return result.response
        else:
            raise RuntimeError(f"Root function error: {result.response}")

    def set_request_status(self, call: ServerCall, in_progress: bool):
        with self.set_request_status_lock:
            if in_progress:
                self.running_actions.append(call)
                self.event_bus.emit(RootHelperClientEvents.ROOT_REQUEST_STATUS, self, call, True)
            else:
                self.running_actions.remove(call)
                self.event_bus.emit(RootHelperClientEvents.ROOT_REQUEST_STATUS, self, call, False)
            if not self.running_actions and not self.keep_unlocked and not self.authorization_keepers and self.server_handshake_established() and self.is_server_process_running:
                self.stop_root_helper()

    def keep_root_unlocked_changed(self, value: bool):
        self.keep_unlocked = value

@final
class RootHelperClientEvents(Enum):
    CHANGE_ROOT_ACCESS = auto() # root_helper_client unlocked / locked root access
    ROOT_REQUEST_STATUS = auto() # calls when state of root_function is changed (in progress / finished)

@final
class ServerCallEvents(Enum):
    NEW_OUTPUT_LINE = auto() # new line added to collected output
    CALL_WILL_TERMINATE = auto()

@dataclass
class ServerCall:
    """Captures details about ongoing server call."""
    """Can be used to join the thread later or send cancel request for single request."""
    request: ServerCommand | ServerFunction
    client: RootHelperClient
    thread: threading.Thread | None = None
    call_id: uuid.UUID = field(default_factory=uuid.uuid4)
    terminated: bool = False # Mark as terminated. Might still be terminating.
    output: list[str] = field(default_factory=list) # Contains output lines from stdout and stderr
    output_lock: threading.Lock = field(default_factory=threading.Lock)
    event_bus: EventBus[ServerCallEvents] = field(default_factory=EventBus[ServerCallEvents])

    def __repr__(self):
        return f"ServerCall(request={self.request.function_name!r})"

    @property
    def is_cancellable(self) -> bool:
        """Only ServerFunctions are cancellable"""
        return isinstance(self.request, ServerFunction)

    def cancel(self):
        """Sends CANCEL_CALL <ID> to server. Can be used only with async calls that already started."""
        def worker():
            if not self.is_cancellable:
                print("[Server process]: Warning: Tried to cancel a call that is not cancellable")
                return
            if self.thread:
                self.client.send_request(ServerCommand.CANCEL_CALL, command_value=str(self.call_id), asynchronous=True)
        threading.Thread(target=worker, daemon=True).start()

    def output_append(self, line: str):
        with self.output_lock:
            self.output.append(line)
            self.event_bus.emit(ServerCallEvents.NEW_OUTPUT_LINE, line)

    def get_output(self) -> list[str]:
        with self.output_lock:
            return self.output

    def mark_terminated(self):
        self.terminated = True
        self.event_bus.emit(ServerCallEvents.CALL_WILL_TERMINATE)

# ------------------------------------------------------------------------------
# Helper functions and types.
# ------------------------------------------------------------------------------

class ServerCallError(Exception):
    """Custom exception with predefined error codes and messages."""
    def __init__(self, error_code: int, message: str):
        # You can pass either a predefined error code or a custom message
        self.error_code = error_code
        self.message = message
        super().__init__(f"Error code {error_code}: {message}")
    def __str__(self):
        return f"Server error (code={self.error_code}): {self.message}"
# Define error codes and their corresponding messages as class variables
ServerCallError.SERVER_NOT_RESPONDING = ServerCallError(1, "The server is not responding.")

class AuthorizationKeeperEvent(Enum):
    RETAIN_COUNTER_REACHED_0 = auto()

@dataclass
class AuthorizationKeeper:
    name: str
    retain_counter: int = 1
    event_bus: EventBus[AuthorizationKeeperEvent] = field(default_factory=EventBus[AuthorizationKeeperEvent])
    lock: threading.Lock = field(default_factory=threading.Lock)
    ignore_released = False # Used only to maintain authorization after manually clicking on lock button

    def retain(self):
        with self.lock:
            if self.retain_counter == 0:
                raise RuntimeError("AuthorizationKeeper already reached 0")
            self.retain_counter += 1

    def release(self):
        with self.lock:
            if self.retain_counter == 0:
                raise RuntimeError("AuthorizationKeeper already reached 0")
            self.retain_counter -= 1
            if self.retain_counter == 0:
                self.event_bus.emit(AuthorizationKeeperEvent.RETAIN_COUNTER_REACHED_0, self)

