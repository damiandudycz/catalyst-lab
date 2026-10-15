# Managing GIT based directory (local or remote). Base for RelengDirectory and
# OverlayDirectory.
from __future__ import annotations
import os, threading, subprocess, uuid
from contextlib import contextmanager
from gi.repository import GLib
from enum import Enum, auto
from datetime import datetime
from .repository import Serializable
from .event_bus import EventBus, SharedEvent
from .status_indicator import StatusIndicatorState, StatusIndicatorValues, StatusDetail, item_status
from abc import ABC, abstractmethod

class GitDirectoryEvent(Enum):
    LOGS_CHANGED = auto()
    NAME_CHANGED = auto()
    CONTENT_CHANGED = auto()

class GitDirectoryStatus(Enum):
    # Git status
    UNKNOWN = auto() # Not checked yet.
    UNCHANGED = auto()
    CHANGED = auto()
    CONFLICTED = auto() # Unfinished merge with conflicts.
    ERROR = auto() # Directory is missing, isn't Git repository, or Git failed.

# Status codes of unmerged files in git status --porcelain.
_CONFLICT_STATUS_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}

class GitDirectory(Serializable, ABC):

    # Overwrite in subclassed
    @classmethod
    @abstractmethod
    def base_location(cls) -> str:
        pass

    def __init__(
        self, name: str,
        id: uuid.UUID | None = None,
        branch_name: str | None = None,
        last_commit_date: datetime | None = None,
        remote_url: str | None = None,
        has_remote_changes: bool = False,
        metadata: Serializable | None = None
    ):
        self.name = name
        self.id = id or uuid.uuid4()
        self.status: GitDirectoryStatus = GitDirectoryStatus.UNKNOWN
        self.last_commit_date = last_commit_date
        self.branch_name = branch_name
        self.remote_url = remote_url
        self.has_remote_changes = has_remote_changes
        self.metadata = metadata
        self.logs: list[dict] = []
        self.event_bus = EventBus[GitDirectoryEvent]()
        # Changes made by app (eg. stages of project saved, renamed or removed) change Git status. Several changes come
        # together (eg. stage added and its file written), status is read once after them.
        self._status_update_id = None
        self.event_bus.subscribe(GitDirectoryEvent.CONTENT_CHANGED, self._on_content_changed)
        # Running Git operations (update, save, discard), shown as blinking status indicator.
        self._operations = 0
        self._operations_lock = threading.Lock()

    @contextmanager
    def operation(self):
        """Marks Git operation running on directory while inside of with block."""
        with self._operations_lock:
            self._operations += 1
        self.event_bus.emit(SharedEvent.STATE_UPDATED, self)
        try:
            yield
        finally:
            with self._operations_lock:
                self._operations -= 1
            self.event_bus.emit(SharedEvent.STATE_UPDATED, self)

    @property
    def is_busy(self) -> bool:
        return self._operations > 0

    @property
    def short_details(self) -> str:
        parts = []
        if self.branch_name:
            parts.append(self.branch_name)
        if self.last_commit_date:
            parts.append(self.last_commit_date.strftime('%Y-%m-%d %H:%M'))
        return ", ".join(parts)

    @property
    def status_indicator_values(self) -> StatusIndicatorValues:
        # TODO: Show updates available (has_remote_changes) too.
        return item_status(self.status_details(), blinking=self.is_busy)

    def status_details(self) -> list[StatusDetail]:
        """States of directory (Git status, running operation)."""
        match self.status:
            case GitDirectoryStatus.CHANGED:
                detail = StatusDetail(StatusIndicatorState.CHANGED, "Not saved changes")
            case GitDirectoryStatus.CONFLICTED:
                detail = StatusDetail(StatusIndicatorState.ERROR, "Merge conflicts")
            case GitDirectoryStatus.ERROR:
                detail = StatusDetail(StatusIndicatorState.ERROR, "Git status can't be read (directory is missing or isn't Git repository)")
            case GitDirectoryStatus.UNKNOWN:
                detail = StatusDetail(StatusIndicatorState.IDLE, "Checking status")
            case _:
                detail = StatusDetail(StatusIndicatorState.IDLE, "No changes")
        return [detail, StatusDetail(StatusIndicatorState.LOADED, "Git operation running") if self.is_busy else None]

    @classmethod
    def parse_metadata(cls, dict: dict) -> Serializable:
        """Overwrite in subclasses that use metadata"""
        return None

    def serialize(self) -> dict:
        return {
            "name": self.name,
            "id": str(self.id),
            "last_commit_date": self.last_commit_date.isoformat() if self.last_commit_date else None,
            "remote_url": self.remote_url,
            "branch_name": self.branch_name,
            "has_remote_changes": self.has_remote_changes,
            "metadata": self.metadata.serialize() if self.metadata else None
        }

    @classmethod
    def init_from(cls, data: dict) -> Self:
        try:
            name = data["name"]
            id_value = uuid.UUID(data["id"])
            last_commit_date = (
                datetime.fromisoformat(data["last_commit_date"])
                if data.get("last_commit_date") else None
            )
            remote_url = data.get("remote_url")
            branch_name = data.get("branch_name")
            has_remote_changes = data.get("has_remote_changes")
            metadata = (
                cls.parse_metadata(dict=data.get("metadata"))
                if data.get("metadata") else None
            )
        except KeyError:
            raise ValueError(f"Failed to parse {data}")
        return cls(
            name=name,
            id=id_value,
            last_commit_date=last_commit_date,
            remote_url=remote_url,
            branch_name=branch_name,
            has_remote_changes=has_remote_changes,
            metadata=metadata
        )

    def _on_content_changed(self, *args):
        if self._status_update_id is not None:
            GLib.source_remove(self._status_update_id)
        def update():
            self._status_update_id = None
            self.update_status()
            return False
        self._status_update_id = GLib.timeout_add(300, update)

    def update_status(self, wait: bool = False):
        def worker():
            directory = self.directory_path()
            if not os.path.isdir(directory) or not os.path.isdir(
                os.path.join(directory, ".git")
            ):
                self.status = GitDirectoryStatus.ERROR
                self.last_commit_date = None
                self.branch_name = None
                self.event_bus.emit(SharedEvent.STATE_UPDATED, self)
                return
            try:
                # Get git status porcelain
                process = subprocess.Popen(
                    ["git", "status", "--porcelain"],
                    cwd=directory,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True
                )
                stdout, _ = process.communicate()
                if process.returncode != 0:
                    self.status = GitDirectoryStatus.ERROR
                elif any(line[:2] in _CONFLICT_STATUS_CODES for line in stdout.splitlines()):
                    self.status = GitDirectoryStatus.CONFLICTED
                else:
                    self.status = GitDirectoryStatus.CHANGED if stdout.strip() else GitDirectoryStatus.UNCHANGED
                # Get last commit date (ISO 8601)
                process_date = subprocess.Popen(
                    ["git", "log", "-1", "--format=%cI"],
                    cwd=directory,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True
                )
                last_commit_date, _ = process_date.communicate()
                if last_commit_date:
                    self.last_commit_date = datetime.fromisoformat(
                        last_commit_date.strip()
                    )
                else:
                    self.last_commit_date = None
                # Get current branch name
                process_branch = subprocess.Popen(
                    ["git", "symbolic-ref", "--short", "HEAD"],
                    cwd=directory,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True
                )
                branch_name, _ = process_branch.communicate()
                self.branch_name = branch_name.strip() if process_branch.returncode == 0 else None
                # Get remote URL
                process_remote = subprocess.Popen(
                    ["git", "remote", "get-url", "origin"],
                    cwd=directory,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True
                )
                remote_url, _ = process_remote.communicate()
                self.remote_url = remote_url.strip() if process_remote.returncode == 0 else None
                # Check if there are remote changes
                try:
                    process_fetch = subprocess.Popen(
                        ["git", "fetch"],
                        cwd=directory,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    process_fetch.wait()
                    if process_fetch.returncode != 0:
                        raise RuntimeError("git fetch failed")
                    process_diff = subprocess.Popen(
                        [
                            "git",
                            "rev-list",
                            "--count",
                            "--left-only",
                            "@{u}...HEAD"
                        ],
                        cwd=directory,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True
                    )
                    output, error = process_diff.communicate()
                    if process_diff.returncode == 0:
                        behind_count = int(output.strip())
                        self.has_remote_changes = behind_count > 0
                    else:
                        self.has_remote_changes = False
                except Exception as e:
                    print(f"Warning: Failed to check for updates: {e}")
                    self.has_remote_changes = False
            except Exception as e:
                print(f"STATUS EXCEPTION: {e}")
                self.status = GitDirectoryStatus.ERROR
                self.last_commit_date = None
                self.branch_name = None
            self.event_bus.emit(SharedEvent.STATE_UPDATED, self)
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        if wait:
            thread.join()

    def update_logs(self, wait: bool = False):
        def worker():
            directory = self.directory_path()
            logs = []
            if not os.path.isdir(directory) or not os.path.isdir(
                os.path.join(directory, ".git")
            ):
                self.logs = []
                self.event_bus.emit(GitDirectoryEvent.LOGS_CHANGED, self.logs)
                return
            try:
                # Use a custom format to make parsing easier
                log_format = "%H%x1f%an%x1f%aI%x1f%s"
                process = subprocess.Popen(
                    [
                        "git",
                        "log",
                        f"--format={log_format}"
                    ],
                    cwd=directory,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True
                )
                stdout, _ = process.communicate()
                for line in stdout.strip().splitlines():
                    parts = line.strip().split('\x1f')
                    if len(parts) == 4:
                        commit_hash, author, date_str, message = parts
                        try:
                            date = datetime.fromisoformat(date_str)
                        except ValueError:
                            date = None
                        logs.append({
                            "hash": commit_hash,
                            "author": author,
                            "date": date,
                            "message": message,
                        })
            except Exception as e:
                print(f"LOG EXCEPTION: {e}")
                self.logs = []
            self.logs = logs
            self.event_bus.emit(GitDirectoryEvent.LOGS_CHANGED, self.logs)
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        if wait:
            thread.join()

    def discard_changes(self, wait: bool = False):
        def worker():
            with self.operation():
                try:
                    subprocess.run(
                        ["git", "reset", "--hard"],
                        cwd=self.directory_path(),
                        check=True
                    )
                    subprocess.run(
                        ["git", "clean", "-fdx"],
                        cwd=self.directory_path(),
                        check=True
                    )
                except Exception as e:
                    print(f"DISCARD EXCEPTION: {e}")
                finally:
                    # Status is read again also after failure, it could be outdated. Operation ends with new status.
                    self.update_status(wait=True)
                    self.update_logs(wait=True)
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        if wait:
            thread.join()

    DEFAULT_COMMIT_MESSAGE = "Save changes"

    def commit_changes(self, message: str | None = None, wait: bool = False):
        """Commits all changes of directory, with given message (default one when empty)."""
        message = (message or "").strip() or self.DEFAULT_COMMIT_MESSAGE
        def worker():
            with self.operation():
                try:
                    subprocess.run(
                        ["git", "add", "-A"],
                        cwd=self.directory_path(),
                        check=True
                    )
                    # Nothing to commit when changes were reverted outside of app (status shown was outdated).
                    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=self.directory_path())
                    if staged.returncode != 0:
                        subprocess.run(
                            ["git", "commit", "-m", message],
                            cwd=self.directory_path(),
                            check=True
                        )
                except Exception as e:
                    print(f"COMMIT EXCEPTION: {e}")
                finally:
                    # Status is read again also after failure, it could be outdated. Operation ends with new status.
                    self.update_status(wait=True)
                    self.update_logs(wait=True)
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        if wait:
            thread.join()

    @classmethod
    def directory_path_for_name(cls, name: str) -> str:
        return os.path.join(
            cls.base_location(),
            cls.sanitized_name_for_name(name)
        )

    def directory_path(self) -> str:
        return self.__class__.directory_path_for_name(self.name)

    @staticmethod
    def sanitized_name_for_name(name: str) -> str:
        def sanitize_filename_linux(name: str) -> str:
            return name.replace('/', '_').replace('\0', '_')
        return sanitize_filename_linux(name=name)

    def sanitized_name(self) -> str:
        return self.sanitized_name_for_name(self.name)

