from __future__ import annotations
from typing import Protocol, Self, TypeVar, Generic, Type, Callable, final
from functools import wraps
import json
import os
from .runtime_env import RuntimeEnv
from .json_serialization import dump_json
from .event_bus import EventBus
from enum import Enum, auto

# Library: folder with all app data (toolsets, snapshots, projects, builds...) and repository metadata. User preferences
# are stored in the system config directory, while the selected Library path is stored there as well.
DEFAULT_LIBRARY_DIRECTORY = "~/CatalystLab"

def resolve_path(path: str) -> str:
    """Expand a configured path and resolve its symlinks."""
    return os.path.realpath(os.path.expanduser(path))

def _system_config_directory() -> str:
    if RuntimeEnv.current() == RuntimeEnv.FLATPAK:
        return os.path.expanduser(f"~/.var/app/{os.environ.get('FLATPAK_ID')}/config")
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")

_library_directory: str | None = None

def library_directory() -> str:
    """Absolute path of library. Read once, app is restarted when library changes."""
    global _library_directory
    if _library_directory is None:
        # The selected Library is now part of the same Settings object as every
        # other preference. Import lazily because repositories.py registers the
        # Settings repository before loading repositories that need this path.
        from .repositories import Repository
        location = Repository.Settings.value.library_location
        _library_directory = os.path.abspath(os.path.expanduser(location))
    return _library_directory

def store_library_directory(path: str):
    """Persist the selected Library and update this process's cached path."""
    global _library_directory
    path = os.path.abspath(os.path.expanduser(path))
    from .repositories import Repository
    Repository.Settings.value.library_location = path
    _library_directory = path

def collapse_home(path: str) -> str:
    """Path in home folder written with ~, so it stays valid when home folder is renamed."""
    home = os.path.expanduser("~")
    path = os.path.abspath(os.path.expanduser(path))
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path

def library_path(*names: str) -> str:
    """Location in library (default locations of settings)."""
    return collapse_home(os.path.join(library_directory(), *names))

def preferences_file() -> str:
    """Per-user preferences, independent of the selected Library."""
    return os.path.join(_system_config_directory(), "catalystlab", "preferences.json")

class Serializable(Protocol):
    def serialize(self) -> dict: ...
    @classmethod
    def init_from(cls, data: dict) -> Self: ...

T = TypeVar("T", bound=Serializable)

def _save_after(method):
    @wraps(method)
    def tracked(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        self._trigger_save()
        return result
    return tracked

class TrackedList(list[T]):
    def __init__(self, initial: list[T], save_callback):
        super().__init__(initial)
        self._save_callback = save_callback

    def _trigger_save(self):
        if callable(self._save_callback):
            self._save_callback()

    append = _save_after(list.append)
    extend = _save_after(list.extend)
    insert = _save_after(list.insert)
    remove = _save_after(list.remove)
    pop = _save_after(list.pop)
    clear = _save_after(list.clear)
    __setitem__ = _save_after(list.__setitem__)
    __delitem__ = _save_after(list.__delitem__)
    __iadd__ = _save_after(list.__iadd__)
    __imul__ = _save_after(list.__imul__)
    sort = _save_after(list.sort)
    reverse = _save_after(list.reverse)

@final
class RepositoryEvent(Enum):
    VALUE_CHANGED = auto()

class Repository(Generic[T]):

    registered_aliases: list[str] = []

    def __init__(self, cls: Type[T], collection: bool = False, alias: str | None = None, default_factory: Callable[[], T] | None = None):
        self._cls = cls
        self._collection = collection
        self._alias = alias or (cls.__name__.lower() if not collection else f"{cls.__name__.lower()}_list")
        self._default_factory = default_factory
        self.event_bus: EventBus[RepositoryEvent] = EventBus[RepositoryEvent]()
        if self._alias in Repository.registered_aliases:
            raise RuntimeError(f"Repository with alias {self._alias} is already in use. Use shared instance for the same values.")
        else:
            Repository.registered_aliases.append(self._alias)
        self._value: T | TrackedList[T] = self._load()

    def save(self):
        try:
            if self._collection:
                self._cls.save_all(self._value)
            else:
                path = preferences_file()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    dump_json(f, self._value.serialize())
        finally:
            self.event_bus.emit(RepositoryEvent.VALUE_CHANGED, self._value)

    def _load(self) -> T | TrackedList[T]:
        if self._collection:
            items = self._cls.load_all()
            return TrackedList(items, self.save)
        path = preferences_file()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return self._cls.init_from(data)
        except (FileNotFoundError, ValueError, AttributeError):
            return self._default_factory()

    def reload(self):
        """Reload values after switching the active Library without saving stale values over it."""
        self._value = self._load()
        self.event_bus.emit(RepositoryEvent.VALUE_CHANGED, self._value)

    @property
    def value(self) -> T | TrackedList[T]:
        return self._value
    @value.setter
    def value(self, new_value: T | list[T]):
        if self._collection:
            if new_value is self._value:
                return
            if isinstance(new_value, TrackedList):
                self._value = new_value
                self._value._save_callback = self.save  # update callback if needed
                self.save()
            elif isinstance(new_value, list):
                self._value = TrackedList(new_value, self.save)
                self.save()
            else:
                raise TypeError("Expected a list or TrackedList of serializable items.")
        else:
            self._value = new_value
            self.save()

