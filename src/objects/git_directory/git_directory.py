# Managing GIT based directory (local or remote). Base for RelengDirectory and
# OverlayDirectory.
from __future__ import annotations
import os, threading, subprocess, uuid, json
from contextlib import contextmanager
from gi.repository import GLib
from enum import Enum, auto
from datetime import datetime
from .repository import Serializable
from .git_identity import git_environment, require_git_identity
from .event_bus import EventBus, SharedEvent
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

def _run_worker(worker, wait: bool):
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    if wait:
        thread.join()

class GitDirectory(Serializable, ABC):

    APP_METADATA_FILE = "catalystlab.json"

    @staticmethod
    def is_git_directory(directory: str) -> bool:
        git_path = os.path.join(directory, ".git")
        return os.path.isdir(directory) and (os.path.isdir(git_path) or os.path.isfile(git_path))

    @classmethod
    def _app_metadata_path(cls, directory: str) -> str:
        git_path = os.path.join(directory, ".git")
        if os.path.isfile(git_path):
            with open(git_path, encoding="utf-8") as file:
                entry = file.readline().strip()
            if not entry.startswith("gitdir:"):
                raise ValueError(f"Invalid Git directory pointer in {git_path}")
            git_path = os.path.normpath(os.path.join(directory, entry.split(":", 1)[1].strip()))
        return os.path.join(git_path, cls.APP_METADATA_FILE)

    @classmethod
    def load_all(cls):
        items = []
        if not os.path.isdir(cls.base_location()):
            return items
        for entry in os.scandir(cls.base_location()):
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            try:
                path = cls._app_metadata_path(entry.path)
                with open(path, encoding="utf-8") as file:
                    items.append(cls.init_from(json.load(file)))
            except (OSError, ValueError) as error:
                # Non-Git folders are not entries in these collections.
                if cls.is_git_directory(entry.path):
                    print(f"Failed to load {cls.__name__} metadata in {entry.path}: {error}")
        return items

    @classmethod
    def save_all(cls, items):
        for item in items:
            directory = item.directory_path()
            if not os.path.isdir(directory):
                continue
            item.save_metadata()

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
        from .status import StatusIndicatorValues, item_status
        return item_status(self.status_details(), blinking=self.is_busy)

    def status_details(self) -> list[StatusDetail]:
        """States of directory (Git status, running operation)."""
        from .status import StatusIndicatorState, StatusDetail
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
        return {"name": self.name, "id": str(self.id),
                "metadata": self.metadata.serialize() if self.metadata else None}

    def save_metadata(self):
        path = self.__class__._app_metadata_path(self.directory_path())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(self.serialize(), file, indent=2)

    @classmethod
    def init_from(cls, data: dict) -> Self:
        try:
            name = data["name"]
            id_value = uuid.UUID(data["id"])
            metadata = (
                cls.parse_metadata(dict=data.get("metadata"))
                if data.get("metadata") else None
            )
        except KeyError:
            raise ValueError(f"Failed to parse {data}")
        return cls(
            name=name,
            id=id_value,
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

    def _run_git(self, *args, **kwargs):
        if args and args[0] == "commit":
            require_git_identity(self.directory_path())
        kwargs["env"] = git_environment(kwargs.get("env"))
        return subprocess.run(["git", *args], cwd=self.directory_path(), **kwargs)

    def run_git_logged(self, arguments: list[str], log, env=None):
        """Run a Git command and send its command line and output to a log callback."""
        log("$ git " + " ".join(arguments))
        result = self._run_git(*arguments, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        for line in result.stdout.splitlines():
            log(line)
        return result

    def update_status(self, wait: bool = False):
        def worker():
            directory = self.directory_path()
            if not self.is_git_directory(directory):
                self.status = GitDirectoryStatus.ERROR
                self.last_commit_date = None
                self.branch_name = None
                self.event_bus.emit(SharedEvent.STATE_UPDATED, self)
                return
            try:
                # Get git status porcelain
                result = self._run_git("status", "--porcelain", stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True)
                if result.returncode != 0:
                    self.status = GitDirectoryStatus.ERROR
                elif any(line[:2] in _CONFLICT_STATUS_CODES for line in result.stdout.splitlines()):
                    self.status = GitDirectoryStatus.CONFLICTED
                else:
                    self.status = GitDirectoryStatus.CHANGED if result.stdout.strip() else GitDirectoryStatus.UNCHANGED
                # Get last commit date (ISO 8601)
                result = self._run_git("log", "-1", "--format=%cI", stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True)
                last_commit_date = result.stdout
                if last_commit_date:
                    self.last_commit_date = datetime.fromisoformat(
                        last_commit_date.strip()
                    )
                else:
                    self.last_commit_date = None
                # Get current branch name
                result = self._run_git("symbolic-ref", "--short", "HEAD", stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True)
                self.branch_name = result.stdout.strip() if result.returncode == 0 else None
                # Get remote URL
                result = self._run_git("remote", "get-url", "origin", stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True)
                self.remote_url = result.stdout.strip() if result.returncode == 0 else None
                # Check if there are remote changes
                try:
                    fetch = self._run_git("fetch", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if fetch.returncode != 0:
                        raise RuntimeError("git fetch failed")
                    diff = self._run_git("rev-list", "--count", "--left-only", "@{u}...HEAD",
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    if diff.returncode == 0:
                        behind_count = int(diff.stdout.strip())
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
        _run_worker(worker, wait)

    def update_logs(self, wait: bool = False):
        def worker():
            directory = self.directory_path()
            logs = []
            if not self.is_git_directory(directory):
                self.logs = []
                self.event_bus.emit(GitDirectoryEvent.LOGS_CHANGED, self.logs)
                return
            try:
                # Use a custom format to make parsing easier
                log_format = "%H%x1f%an%x1f%aI%x1f%s"
                result = self._run_git("log", f"--format={log_format}", stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True)
                for line in result.stdout.strip().splitlines():
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
        _run_worker(worker, wait)

    def _run_change(self, name, action, wait: bool):
        def worker():
            with self.operation():
                try:
                    action()
                except Exception as error:
                    print(f"{name} EXCEPTION: {error}")
                finally:
                    self.update_status(wait=True)
                    self.update_logs(wait=True)
        _run_worker(worker, wait)

    def discard_changes(self, wait: bool = False):
        def discard():
            self._run_git("reset", "--hard", check=True)
            self._run_git("clean", "-fdx", check=True)
        self._run_change("DISCARD", discard, wait)

    DEFAULT_COMMIT_MESSAGE = "Save changes"

    def commit_changes(self, message: str | None = None, wait: bool = False):
        """Commits all changes of directory, with given message (default one when empty)."""
        message = (message or "").strip() or self.DEFAULT_COMMIT_MESSAGE
        def commit():
            self._run_git("add", "-A", check=True)
            # Nothing to commit when changes were reverted outside of app (status shown was outdated).
            if self._run_git("diff", "--cached", "--quiet").returncode != 0:
                self._run_git("commit", "-m", message, check=True)
        self._run_change("COMMIT", commit, wait)

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

