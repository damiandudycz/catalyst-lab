"""Execution endpoints that provide access to the primary build tools and toolsets.

Runner kinds are deliberately transport-neutral so SSH-backed runners can be added
without making toolsets depend on a particular machine implementation.
"""
from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable
from uuid import UUID
import threading

from .event_bus import EventBus, SharedEvent


class RunnerType(str, Enum):
    HOST = "host"
    VM = "vm"
    SSH = "ssh"  # Reserved for a future remote runner implementation.


@runtime_checkable
class Runner(Protocol):
    """Execution endpoint contract consumed by toolsets and primary operations.

    A runner supplies a stable identity, a display name, a kind, and a command
    executor. VM-specific lifecycle and workspace operations remain on VMRunner;
    a future SSH implementation can provide its own transport behind executor.
    """

    id: UUID | str
    name: str
    runner_type: RunnerType

    @property
    def executor(self): ...

    def prepare(self, output_handler=print, process_holder=None, user="Operation"): ...
    def release(self, user="Operation"): ...


class HostRunner:
    """The current computer as an execution endpoint (enabled on Linux)."""

    id = "host"
    name = "Host"
    runner_type = RunnerType.HOST
    short_details = "Runs primary tools directly on this Linux computer"
    icon_name = "computer-symbolic"

    def __init__(self):
        self._usage_lock = threading.RLock()
        self._users: list[str] = []
        self._exclusive_user: str | None = None
        self.event_bus = EventBus()

    @property
    def users(self) -> list[str]:
        with self._usage_lock:
            return list(self._users)

    @property
    def is_used(self) -> bool:
        with self._usage_lock:
            return bool(self._users)

    @property
    def executor(self):
        # Lazy import avoids a dependency cycle: rootless executors use Repository.
        from .rootless import LOCAL_EXECUTOR
        return LOCAL_EXECUTOR

    @classmethod
    def supported(cls) -> bool:
        import sys
        return sys.platform.startswith("linux")

    def prepare(self, output_handler=print, process_holder=None, user="Operation", exclusive=False):
        if not self.supported():
            raise RuntimeError("Host runner is available only on Linux")
        with self._usage_lock:
            if self._exclusive_user and self._exclusive_user != user:
                raise RuntimeError(f"Host runner is reserved by {self._exclusive_user}")
            if exclusive and self._users:
                raise RuntimeError(f"Host runner is in use by {', '.join(self._users)}")
            self._users.append(user)
            if exclusive:
                self._exclusive_user = user
        try:
            output_handler("Prepare Runner: Host")
            self.event_bus.emit(SharedEvent.STATE_UPDATED, self)
        except Exception:
            self.release(user)
            raise

    def release(self, user="Operation"):
        with self._usage_lock:
            try:
                self._users.remove(user)
            except ValueError:
                return False
            if self._exclusive_user == user:
                self._exclusive_user = None
        self.event_bus.emit(SharedEvent.STATE_UPDATED, self)
        return True


HOST_RUNNER = HostRunner()


def runner_by_id(identifier):
    """Resolve a configured runner id, including the Host singleton."""
    if identifier in (None, "host"):
        return HOST_RUNNER if identifier == "host" else None
    from .repository import Repository
    return next((runner for runner in Repository.Runner.value if str(runner.id) == str(identifier)), None)


def default_runner():
    """Return the configured runner when available, otherwise the platform default."""
    from .repository import Repository
    from .lima import virtual_machines_supported

    settings = Repository.Settings.value
    configured = runner_by_id(settings.default_runner_id)
    if configured is not None and runner_is_supported(configured):
        return configured
    if HOST_RUNNER.supported():
        return HOST_RUNNER
    if virtual_machines_supported():
        runners = Repository.Runner.value
        return next((runner for runner in runners if runner_is_supported(runner)), None)
    return None


def runner_is_supported(runner) -> bool:
    """Whether this runner kind is enabled on the current platform."""
    if getattr(runner, "runner_type", None) == RunnerType.HOST:
        return HOST_RUNNER.supported()
    if getattr(runner, "runner_type", None) == RunnerType.VM:
        from .lima import virtual_machines_supported
        return virtual_machines_supported()
    return False
