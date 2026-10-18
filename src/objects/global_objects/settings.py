from __future__ import annotations
from enum import Enum, auto
from typing import final
from .event_bus import EventBus
from .repository import Serializable, Repository, library_path, DEFAULT_LIBRARY_DIRECTORY, collapse_home


@final
class SettingsEvents(Enum):
    KEEP_ROOT_UNLOCKED_CHANGED = auto()
    INITIAL_SETUP_DONE_CHANGED = auto()
    AUTO_CLEAN_SNAPSHOTS_CHANGED = auto()
    NAVIGATION_STYLE_CHANGED = auto()
    PAUSE_QUEUE_ON_FAILURE_CHANGED = auto()
    DEFAULT_TOOLSET_CHANGED = auto()
    DEFAULT_RUNNER_CHANGED = auto()


@final
class NavigationStyle(Enum):
    """How views like wizards and installations are shown."""
    MODAL = "modal"  # In separate windows over main window.
    PUSH = "push"    # Pushed in main window, closing them goes back.


@final
class Settings(Serializable):
    """User preferences. Library data paths are derived from the selected Library."""

    def __init__(
        self,
        initial_setup_done: bool = False,
        keep_root_unlocked: bool = True,
        auto_clean_snapshots: bool = False,
        navigation_style: NavigationStyle = NavigationStyle.PUSH,
        pause_queue_on_failure: bool = False,
        qemu_memory_gib: int = 4,
        qemu_cpus: int = 4,
        default_toolset_id: str | None = None,
        default_runner_id: str | None = None,
        library_location: str | None = None,
    ):
        self._initial_setup_done = initial_setup_done
        self._keep_root_unlocked = keep_root_unlocked
        self._auto_clean_snapshots = auto_clean_snapshots
        self._navigation_style = navigation_style
        self._pause_queue_on_failure = pause_queue_on_failure
        self._qemu_memory_gib = qemu_memory_gib
        self._qemu_cpus = qemu_cpus
        self._default_toolset_id = default_toolset_id
        self._default_runner_id = default_runner_id
        # None lets older preferences files fall back to the default Library path.
        self._library_location = library_location
        self.event_bus = EventBus[SettingsEvents]()

    @classmethod
    def init_from(cls, data: dict) -> Settings:
        return cls(
            initial_setup_done=data.get("initial_setup_done", False),
            keep_root_unlocked=data.get("keep_root_unlocked", True),
            auto_clean_snapshots=data.get("auto_clean_snapshots", False),
            navigation_style=_navigation_style(data.get("navigation_style")),
            pause_queue_on_failure=data.get("pause_queue_on_failure", False),
            qemu_memory_gib=data.get("qemu_memory_gib", 4),
            qemu_cpus=data.get("qemu_cpus", 4),
            default_toolset_id=data.get("default_toolset_id"),
            default_runner_id=data.get("default_runner_id"),
            library_location=data.get("library_location"),
        )

    def serialize(self) -> dict:
        return {
            "initial_setup_done": self.initial_setup_done,
            "keep_root_unlocked": self.keep_root_unlocked,
            "auto_clean_snapshots": self.auto_clean_snapshots,
            "navigation_style": self.navigation_style.value,
            "pause_queue_on_failure": self.pause_queue_on_failure,
            "qemu_memory_gib": self.qemu_memory_gib,
            "qemu_cpus": self.qemu_cpus,
            "default_toolset_id": self.default_toolset_id,
            "default_runner_id": self.default_runner_id,
            "library_location": collapse_home(self.library_location),
        }

    # Data folders are fixed within the selected Library; these are convenience
    # accessors for existing consumers, not user preferences or persisted values.
    @property
    def toolsets_location(self) -> str:
        return library_path("Toolsets")

    @property
    def snapshots_location(self) -> str:
        return library_path("Snapshots")

    @property
    def releng_location(self) -> str:
        return library_path("Releng")

    @property
    def overlay_location(self) -> str:
        return library_path("Overlays")

    @property
    def project_location(self) -> str:
        return library_path("Projects")

    @property
    def builds_location(self) -> str:
        return library_path("Builds")

    @property
    def runners_location(self) -> str:
        return library_path("Runners")

    @property
    def library_location(self) -> str:
        return self._library_location or DEFAULT_LIBRARY_DIRECTORY

    @library_location.setter
    def library_location(self, value: str):
        value = collapse_home(value)
        self._set("library_location", value)

    @property
    def cache_location(self) -> str:
        return library_path("Cache")

    @property
    def temporary_location(self) -> str:
        return library_path("Temporary")

    @property
    def packages_location(self) -> str:
        return library_path("Packages")

    def _set(self, name: str, value, event: SettingsEvents | None = None):
        attribute = f"_{name}"
        if getattr(self, attribute) == value:
            return
        setattr(self, attribute, value)
        if event:
            self.event_bus.emit(event, value)
        Repository.Settings.save()

    @property
    def initial_setup_done(self) -> bool:
        return self._initial_setup_done

    @initial_setup_done.setter
    def initial_setup_done(self, value: bool):
        self._set("initial_setup_done", value, SettingsEvents.INITIAL_SETUP_DONE_CHANGED)

    @property
    def keep_root_unlocked(self) -> bool:
        return self._keep_root_unlocked

    @keep_root_unlocked.setter
    def keep_root_unlocked(self, value: bool):
        self._set("keep_root_unlocked", value, SettingsEvents.KEEP_ROOT_UNLOCKED_CHANGED)

    @property
    def auto_clean_snapshots(self) -> bool:
        return self._auto_clean_snapshots

    @auto_clean_snapshots.setter
    def auto_clean_snapshots(self, value: bool):
        self._set("auto_clean_snapshots", value, SettingsEvents.AUTO_CLEAN_SNAPSHOTS_CHANGED)

    @property
    def navigation_style(self) -> NavigationStyle:
        return self._navigation_style

    @navigation_style.setter
    def navigation_style(self, value: NavigationStyle):
        self._set("navigation_style", value, SettingsEvents.NAVIGATION_STYLE_CHANGED)

    @property
    def pause_queue_on_failure(self) -> bool:
        return self._pause_queue_on_failure

    @pause_queue_on_failure.setter
    def pause_queue_on_failure(self, value: bool):
        self._set("pause_queue_on_failure", value, SettingsEvents.PAUSE_QUEUE_ON_FAILURE_CHANGED)

    @property
    def qemu_memory_gib(self) -> int:
        return self._qemu_memory_gib

    @qemu_memory_gib.setter
    def qemu_memory_gib(self, value: int):
        self._set("qemu_memory_gib", int(value))

    @property
    def qemu_cpus(self) -> int:
        return self._qemu_cpus

    @qemu_cpus.setter
    def qemu_cpus(self, value: int):
        self._set("qemu_cpus", int(value))

    @property
    def default_toolset_id(self) -> str | None:
        return self._default_toolset_id

    @default_toolset_id.setter
    def default_toolset_id(self, value: str | None):
        self._set("default_toolset_id", str(value) if value is not None else None,
                  SettingsEvents.DEFAULT_TOOLSET_CHANGED)

    @property
    def default_runner_id(self) -> str | None:
        return self._default_runner_id

    @default_runner_id.setter
    def default_runner_id(self, value: str | None):
        self._set("default_runner_id", str(value) if value is not None else None,
                  SettingsEvents.DEFAULT_RUNNER_CHANGED)


def _navigation_style(value) -> NavigationStyle:
    try:
        return NavigationStyle(value)
    except (TypeError, ValueError):
        return NavigationStyle.PUSH
