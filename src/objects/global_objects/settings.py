from __future__ import annotations
from enum import Enum, auto
from typing import final
from .event_bus import EventBus
from .repository import Serializable, Repository, library_path


@final
class SettingsEvents(Enum):
    KEEP_ROOT_UNLOCKED_CHANGED = auto()
    INITIAL_SETUP_DONE_CHANGED = auto()
    AUTO_CLEAN_SNAPSHOTS_CHANGED = auto()
    NAVIGATION_STYLE_CHANGED = auto()
    PAUSE_QUEUE_ON_FAILURE_CHANGED = auto()


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
    ):
        self._initial_setup_done = initial_setup_done
        self._keep_root_unlocked = keep_root_unlocked
        self._auto_clean_snapshots = auto_clean_snapshots
        self._navigation_style = navigation_style
        self._pause_queue_on_failure = pause_queue_on_failure
        self._qemu_memory_gib = qemu_memory_gib
        self._qemu_cpus = qemu_cpus
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
    def cache_location(self) -> str:
        return library_path("Cache")

    @property
    def temporary_location(self) -> str:
        return library_path("Temporary")

    @property
    def packages_location(self) -> str:
        return library_path("Packages")

    @property
    def initial_setup_done(self) -> bool:
        return self._initial_setup_done

    @initial_setup_done.setter
    def initial_setup_done(self, value: bool):
        if self._initial_setup_done != value:
            self._initial_setup_done = value
            self.event_bus.emit(SettingsEvents.INITIAL_SETUP_DONE_CHANGED, value)
            Repository.Settings.save()

    @property
    def keep_root_unlocked(self) -> bool:
        return self._keep_root_unlocked

    @keep_root_unlocked.setter
    def keep_root_unlocked(self, value: bool):
        if self._keep_root_unlocked != value:
            self._keep_root_unlocked = value
            self.event_bus.emit(SettingsEvents.KEEP_ROOT_UNLOCKED_CHANGED, value)
            Repository.Settings.save()

    @property
    def auto_clean_snapshots(self) -> bool:
        return self._auto_clean_snapshots

    @auto_clean_snapshots.setter
    def auto_clean_snapshots(self, value: bool):
        if self._auto_clean_snapshots != value:
            self._auto_clean_snapshots = value
            self.event_bus.emit(SettingsEvents.AUTO_CLEAN_SNAPSHOTS_CHANGED, value)
            Repository.Settings.save()

    @property
    def navigation_style(self) -> NavigationStyle:
        return self._navigation_style

    @navigation_style.setter
    def navigation_style(self, value: NavigationStyle):
        if self._navigation_style != value:
            self._navigation_style = value
            self.event_bus.emit(SettingsEvents.NAVIGATION_STYLE_CHANGED, value)
            Repository.Settings.save()

    @property
    def pause_queue_on_failure(self) -> bool:
        return self._pause_queue_on_failure

    @pause_queue_on_failure.setter
    def pause_queue_on_failure(self, value: bool):
        if self._pause_queue_on_failure != value:
            self._pause_queue_on_failure = value
            self.event_bus.emit(SettingsEvents.PAUSE_QUEUE_ON_FAILURE_CHANGED, value)
            Repository.Settings.save()

    @property
    def qemu_memory_gib(self) -> int:
        return self._qemu_memory_gib

    @qemu_memory_gib.setter
    def qemu_memory_gib(self, value: int):
        value = int(value)
        if self._qemu_memory_gib != value:
            self._qemu_memory_gib = value
            Repository.Settings.save()

    @property
    def qemu_cpus(self) -> int:
        return self._qemu_cpus

    @qemu_cpus.setter
    def qemu_cpus(self, value: int):
        value = int(value)
        if self._qemu_cpus != value:
            self._qemu_cpus = value
            Repository.Settings.save()


def _navigation_style(value) -> NavigationStyle:
    try:
        return NavigationStyle(value)
    except (TypeError, ValueError):
        return NavigationStyle.PUSH
