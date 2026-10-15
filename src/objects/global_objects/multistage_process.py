from __future__ import annotations
import sys, threading
from gi.repository import GLib
from typing import final
from enum import Enum, auto
from abc import ABC, abstractmethod
from .event_bus import EventBus
from .root_helper_client import AuthorizationKeeper, ServerCall, ServerCallEvents

# ------------------------------------------------------------------------------
# Process/Stage state:
# ------------------------------------------------------------------------------

@final
class MultiStageProcessState(Enum):
    """Current state of installation."""
    SETUP = auto()       # Process not started yet.
    IN_PROGRESS = auto() # In progress.
    COMPLETED = auto()   # Completed sucessfully.
    FAILED = auto()      # Failed at any of steps.

@final
class MultiStageProcessStageState(Enum):
    """Stage of single stage step."""
    SCHEDULED = auto()   # Step scheduled for execution.
    IN_PROGRESS = auto() # Step started and is in progress.
    COMPLETED = auto()   # Step completed successfully.
    FAILED = auto()      # Step failed.

# ------------------------------------------------------------------------------
# Process/Stage events:
# ------------------------------------------------------------------------------

@final
class MultiStageProcessEvent(Enum):
    """Events produced by MultiStageProcess."""
    # Instance events:
    STATE_CHANGED = auto()
    PROGRESS_CHANGED = auto()
    # Class events:
    STARTED_PROCESSES_CHANGED = auto()

@final
class MultiStageProcessStageEvent(Enum):
    """Events produced by single steps."""
    STATE_CHANGED = auto()
    PROGRESS_CHANGED = auto()
    OUTPUT_LINE_ADDED = auto()

# ------------------------------------------------------------------------------
# MultiStageProcess base class:
# ------------------------------------------------------------------------------

class MultiStageProcess(ABC):
    """Abstract class for managing processes made of multiple stages."""
    """For example toolset installation, snapshot creation, etc."""

    # TODO: Send and receive events about this with class information, to display only for given classes
    started_processes: list[MultiStageProcess] = [] # List of processes that were started. Processes remain there even after success/failure until cleared.
    # Completed processes (removed from started ones) whose result wasn't seen yet, for statuses of sections.
    finished_processes: list[MultiStageProcess] = []
    event_bus = EventBus[MultiStageProcessEvent]() # For class events.

    def __init__(self, title: str):
        """Call super().__init__() at the end of implementation."""
        self.title = title
        self.event_bus = EventBus[MultiStageProcessEvent]() # For instance events.
        self.authorization_keeper: AuthorizationKeeper | None = None # Set in start().
        self.status = MultiStageProcessState.SETUP # Changes to IN_PROGRESS in start().
        self.progress: float = 0.0 # Calculated automatically from all stages.
        self.stages: list[MultiStageProcessStage] = [] # Set in setup_stages.
        self.setup_stages()
        self.observe_stages_progress()

    @abstractmethod
    def setup_stages(self):
        # TODO: No need to call super in direct subclasses
        pass

    def observe_stages_progress(self):
        for stage in self.stages:
            stage.event_bus.subscribe(
                MultiStageProcessStageEvent.PROGRESS_CHANGED,
                self._update_progress
            )

    def start(self, authorization_keeper: AuthorizationKeeper | None = None):
        try:
            if authorization_keeper:
                authorization_keeper.retain()
            self.authorization_keeper = authorization_keeper
            self.status = MultiStageProcessState.IN_PROGRESS
            self.event_bus.emit(MultiStageProcessEvent.STATE_CHANGED, self.status)
            MultiStageProcess.started_processes.append(self)
            MultiStageProcess.event_bus.emit(
                MultiStageProcessEvent.STARTED_PROCESSES_CHANGED,
                self.__class__,
                MultiStageProcess.get_started_processes_by_class(self.__class__)
            )
        except Exception as e:
            print(f"Failed to start {self.title}: {e}")
            self.cancel()
        finally:
            self._continue_process()

    def cancel(self):
        self.status = MultiStageProcessState.FAILED if self.status == MultiStageProcessState.IN_PROGRESS else MultiStageProcessState.SETUP
        self.event_bus.emit(MultiStageProcessEvent.STATE_CHANGED, self.status)
        running_stage = next((stage for stage in self.stages if stage.state == MultiStageProcessStageState.IN_PROGRESS), None)
        if running_stage:
            running_stage.cancel()
        try:
            self._cleanup()
        except Exception as e:
            print(e)
        finally:
            self.complete_process(success=False)

    @abstractmethod
    def complete_process(self, success: bool):
        pass

    def mark_result_seen(self):
        """Finished process was shown (its view was visible when it finished, or was opened later): side menu doesn't
        list it anymore."""
        if getattr(self, "result_seen", False):
            return
        self.result_seen = True
        if self in MultiStageProcess.finished_processes:
            MultiStageProcess.finished_processes.remove(self)
        if hook := getattr(self, "on_result_seen", None):
            hook() # Eg. status of project shown with its result.
        MultiStageProcess.event_bus.emit(
            MultiStageProcessEvent.STARTED_PROCESSES_CHANGED,
            self.__class__,
            MultiStageProcess.get_started_processes_by_class(self.__class__)
        )

    def clean_from_started_processes(self):
        if self.status == MultiStageProcessState.COMPLETED or self.status == MultiStageProcessState.FAILED or self.status == MultiStageProcessState.SETUP:
            if self in MultiStageProcess.started_processes:
                MultiStageProcess.started_processes.remove(self)
                MultiStageProcess.event_bus.emit(
                    MultiStageProcessEvent.STARTED_PROCESSES_CHANGED,
                    self.__class__,
                    MultiStageProcess.get_started_processes_by_class(self.__class__)
                )

    def _update_progress(self, stage_progress: float | None):
        self.progress = sum(stage.progress or 0 for stage in self.stages) / len(self.stages)
        self.event_bus.emit(MultiStageProcessEvent.PROGRESS_CHANGED, self.progress)

    def _cleanup(self):
        for stage in reversed(self.stages): # Cleanup in reverse order
            stage.cleanup()
        if self.authorization_keeper:
            self.authorization_keeper.release()
            self.authorization_keeper = None

    def _continue_process(self):
        if self.status == MultiStageProcessState.COMPLETED or self.status == MultiStageProcessState.FAILED or self.status == MultiStageProcessState.SETUP:
            return # Prevents displaying multiple failure messages in some cases.
        next_stage = next((stage for stage in self.stages if stage.state == MultiStageProcessStageState.SCHEDULED), None)
        failed_stage = next((stage for stage in self.stages if stage.state == MultiStageProcessStageState.FAILED), None)
        if failed_stage:
            self.status = MultiStageProcessState.FAILED
            self.event_bus.emit(MultiStageProcessEvent.STATE_CHANGED, self.status)
            try:
                self._cleanup()
            except Exception as e:
                print(e)
            finally:
                self.complete_process(success=False)
        elif next_stage:
            next_step_thread = threading.Thread(target=next_stage.start)
            next_step_thread.start()
        else:
            self.status = MultiStageProcessState.COMPLETED
            if not getattr(self, "result_seen", False):
                MultiStageProcess.finished_processes.append(self)
            self.event_bus.emit(MultiStageProcessEvent.STATE_CHANGED, self.status)
            try:
                self._cleanup()
            except Exception as e:
                print(e)
            finally:
                MultiStageProcess.started_processes.remove(self)
                MultiStageProcess.event_bus.emit(
                    MultiStageProcessEvent.STARTED_PROCESSES_CHANGED,
                    self.__class__,
                    MultiStageProcess.get_started_processes_by_class(self.__class__)
                )
                self.complete_process(success=True)

    @classmethod
    def get_started_processes_by_class(cls, process_class: type[MultiStageProcess]) -> list[MultiStageProcess]:
        return [p for p in cls.started_processes if isinstance(p, process_class)]

# ------------------------------------------------------------------------------
# MultiStageProcessStage base class:
# ------------------------------------------------------------------------------

class MultiStageProcessStage(ABC):
    """Base class for MultiStageProcess stages."""
    def __init__(self, name: str, description: str, multistage_process: MultiStageProcess):
        self.state = MultiStageProcessStageState.SCHEDULED
        self.name = name
        self.description = description
        self.multistage_process = multistage_process
        self.progress: float | None = None
        self.event_bus = EventBus[MultiStageProcessStageEvent]()
        self._cancel_event = threading.Event()
        self.output_lines: list[str] = [] # Output of commands executed by this stage, displayed in execution view.
        self._server_call: ServerCall | None = None
    @property
    def server_call(self) -> ServerCall | None:
        return self._server_call
    @server_call.setter
    def server_call(self, server_call: ServerCall | None):
        """Output of every server call assigned here is collected in output_lines."""
        if server_call is not None and server_call is not self._server_call:
            with server_call.output_lock:
                # Lines produced before assignment are copied, next ones arrive through event.
                for line in server_call.output:
                    self.log(line)
                server_call.event_bus.subscribe(ServerCallEvents.NEW_OUTPUT_LINE, self.log)
        self._server_call = server_call
    def log(self, line: str):
        """Adds line to stage output. Can be used directly by stages running local processes."""
        self.output_lines.append(line)
        self.event_bus.emit(MultiStageProcessStageEvent.OUTPUT_LINE_ADDED, line)
    @abstractmethod
    def start(self):
        self._cancel_event.clear()
        self._update_state(MultiStageProcessStageState.IN_PROGRESS)
    def cancel(self):
        if self.state == MultiStageProcessStageState.IN_PROGRESS:
            self.complete(MultiStageProcessStageState.FAILED)
            self._cancel_event.set()
    def cleanup(self) -> bool:
        """Returns true if cleanup was needed and was started."""
        if self.state == MultiStageProcessStageState.SCHEDULED:
            return False # No cleaning needed if job didn't start.
        self.cancel()
        print(f"::: Clean {self.name}")
        return True
    def complete(self, state: MultiStageProcessStageState):
        """Call this when step finishes."""
        if self._cancel_event.is_set():
            return
        if state == MultiStageProcessStageState.FAILED:
            # Steps report failures from their except blocks, so the exception being handled
            # is the reason of failure. Add it to output, to make it visible in UI.
            error = sys.exc_info()[1]
            if error is not None:
                self.log(f"Error: {error}" if str(error) else f"Error: {type(error).__name__}")
        self._update_state(state=state)
        if self.state == MultiStageProcessStageState.COMPLETED:
           self._update_progress(1.0)
        # Continue process
        GLib.idle_add(self.multistage_process._continue_process)
    def _update_state(self, state: MultiStageProcessStageState):
        self.state = state
        self.event_bus.emit(MultiStageProcessStageEvent.STATE_CHANGED, state)
    def _update_progress(self, progress: float | None):
        self.progress = progress
        self.event_bus.emit(MultiStageProcessStageEvent.PROGRESS_CHANGED, progress)

