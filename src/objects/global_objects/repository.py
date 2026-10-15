from __future__ import annotations
from typing import Protocol, Self, TypeVar, Generic, Type
import json
import os
from .runtime_env import RuntimeEnv
from .event_bus import EventBus
from enum import Enum, auto
from typing import final

# Library: folder with all data of app (toolsets, snapshots, projects, builds...) and its configuration. User can
# keep it in other place (Settings section), its location is stored outside of it.
DEFAULT_LIBRARY_DIRECTORY = "~/CatalystLab"

def _system_config_directory() -> str:
    if RuntimeEnv.current() == RuntimeEnv.FLATPAK:
        return os.path.expanduser(f"~/.var/app/{os.environ.get('FLATPAK_ID')}/config")
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")

def _library_location_file() -> str:
    return os.path.join(_system_config_directory(), "catalystlab-library.json")

_library_directory: str | None = None

def library_directory() -> str:
    """Absolute path of library. Read once, app is restarted when library changes."""
    global _library_directory
    if _library_directory is None:
        location = DEFAULT_LIBRARY_DIRECTORY
        try:
            with open(_library_location_file(), encoding="utf-8") as file:
                location = json.load(file).get("library") or location
        except (OSError, ValueError, AttributeError):
            pass
        _library_directory = os.path.abspath(os.path.expanduser(location))
    return _library_directory

def store_library_directory(path: str):
    """Library used after app restarts."""
    os.makedirs(_system_config_directory(), exist_ok=True)
    temporary = _library_location_file() + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump({"library": collapse_home(path)}, file, indent=4)
    os.replace(temporary, _library_location_file())

def collapse_home(path: str) -> str:
    """Path in home folder written with ~, so it stays valid when home folder is renamed."""
    home = os.path.expanduser("~")
    path = os.path.abspath(os.path.expanduser(path))
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path

def library_path(*names: str) -> str:
    """Location in library (default locations of settings)."""
    return collapse_home(os.path.join(library_directory(), *names))

def config_directory() -> str:
    """Stored repositories and settings, in library with all other data, so removing library resets app."""
    directory = os.path.join(library_directory(), "Config")
    _migrate_legacy_config(directory)
    return directory

def _legacy_config_directory() -> str:
    """Config directory used by older versions."""
    return os.path.join(_system_config_directory(), "catalystlab")

_legacy_config_checked = False

def _migrate_legacy_config(directory: str):
    """Config of older versions is moved to config directory, when data it describes still exists (any location from
    its settings). Otherwise it describes removed data and is deleted."""
    global _legacy_config_checked
    if _legacy_config_checked:
        return
    _legacy_config_checked = True
    legacy = _legacy_config_directory()
    if not os.path.isdir(legacy) or os.path.isdir(directory):
        return
    import shutil
    try:
        with open(os.path.join(legacy, "settings.json"), encoding="utf-8") as file:
            settings = json.load(file)
        locations = [value for key, value in settings.items() if key.endswith("_location") and isinstance(value, str)]
    except (OSError, ValueError, AttributeError):
        locations = []
    locations.append(library_directory())
    if any(os.path.isdir(os.path.expanduser(location)) for location in locations):
        print(f"Moving configuration from {legacy} to {directory}")
        os.makedirs(os.path.dirname(directory), exist_ok=True)
        shutil.move(legacy, directory)
    else:
        print(f"Removing configuration of removed data {legacy}")
        shutil.rmtree(legacy, ignore_errors=True)

class Serializable(Protocol):
    def serialize(self) -> dict: ...
    @classmethod
    def init_from(cls, data: dict) -> Self: ...

T = TypeVar("T", bound=Serializable)

class TrackedList(list[T]):
    def __init__(self, initial: list[T], save_callback):
        super().__init__(initial)
        self._save_callback = save_callback

    def _trigger_save(self):
        if callable(self._save_callback):
            self._save_callback()

    def append(self, item: T):
        super().append(item)
        self._trigger_save()

    def extend(self, iterable):
        super().extend(iterable)
        self._trigger_save()

    def insert(self, index: int, item: T):
        super().insert(index, item)
        self._trigger_save()

    def remove(self, item: T):
        super().remove(item)
        self._trigger_save()

    def pop(self, index: int = -1):
        item = super().pop(index)
        self._trigger_save()
        return item

    def clear(self):
        super().clear()
        self._trigger_save()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._trigger_save()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._trigger_save()

    def sort(self, *args, **kwargs):
        super().sort(*args, **kwargs)
        self._trigger_save()

    def reverse(self):
        super().reverse()
        self._trigger_save()

@final
class RepositoryEvent(Enum):
    VALUE_CHANGED = auto()

class Repository(Generic[T]):

    registered_aliases: list[str] = []

    def __init__(self, cls: Type[T], collection: bool = False, alias: str | None = None, default_factory: Callable[[None],T] | None = None):
        self._cls = cls
        self._collection = collection
        self._alias = alias or (cls.__name__.lower() if not collection else f"{cls.__name__.lower()}_list")
        self._default_factory = default_factory
        self.event_bus: EventBus[RepositoryEvent] = EventBus[RepositoryEvent]()
        self.broken_items: list[BrokenRepositoryItem] = []
        if self._alias in Repository.registered_aliases:
            raise RuntimeError(f"Repository with alias {self._alias} is already in use. Use shared instance for the same values.")
        else:
            Repository.registered_aliases.append(self._alias)
        self._value: T | TrackedList[T] = self._load()

    def _config_file(self) -> str:
        return os.path.join(config_directory(), f"{self._alias}.json")

    def save(self):
        path = self._config_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                if self._collection:
                    if not isinstance(self._value, list):
                        raise TypeError("Expected a list of serializable items.")
                    items = [item.serialize() for item in self._value]
                    items.extend(item.data for item in self.broken_items)
                    json.dump({"items": items}, f, indent=2)
                else:
                    json.dump(self._value.serialize(), f, indent=2)
        except Exception as e:
            raise e
        finally:
            self.event_bus.emit(RepositoryEvent.VALUE_CHANGED, self._value)

    def _load(self) -> T | TrackedList[T]:
        path = self._config_file()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if self._collection:
                items_data = data.get("items", [])
                items = []
                self.broken_items = []
                for item_data in items_data:
                    try:
                        items.append(self._cls.init_from(item_data))
                    except Exception as error:
                        self.broken_items.append(BrokenRepositoryItem(item_data, error))
                return TrackedList(items, self.save)
            else:
                return self._cls.init_from(data)
        except (FileNotFoundError, json.JSONDecodeError, ValueError, AttributeError):
            return TrackedList([], self.save) if self._collection else self._default_factory()

    def _delete(self):
        path = self._config_file()
        if os.path.isfile(path):
            os.remove(path)
        self.broken_items = []
        self._value = TrackedList([], self.save) if self._collection else self._default_factory()

    def reset(self):
        self._delete()

    @property
    def value(self) -> T | TrackedList[T]:
        return self._value
    @value.setter
    def value(self, new_value: T | list[T]):
        if self._collection:
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


class BrokenRepositoryItem:
    """Raw collection entry that failed to load, kept visible and preserved when the repository is saved."""

    def __init__(self, data: dict, error: Exception):
        self.data = data
        self.name = data.get("name") or data.get("path") or data.get("id") or "Unknown item"
        self.error = f"{type(error).__name__}: {error}"

