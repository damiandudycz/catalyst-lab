from __future__ import annotations
from typing import final
from enum import Enum, auto
from .event_bus import EventBus
from .repository import Serializable, Repository, library_path

@final
class SettingsEvents(Enum):
    KEEP_ROOT_UNLOCKED_CHANGED = auto()
    TOOLSETS_LOCATION_CHANGED = auto()
    SNAPSHOTS_LOCATION_CHANGED = auto()
    RELENG_LOCATION_CHANGED = auto()
    OVERLAY_LOCATION_CHANGED = auto()
    PROJECT_LOCATION_CHANGED = auto()
    BUILDS_LOCATION_CHANGED = auto()
    CACHE_LOCATION_CHANGED = auto()
    TEMPORARY_LOCATION_CHANGED = auto()
    PACKAGES_LOCATION_CHANGED = auto()
    INITIAL_SETUP_DONE_CHANGED = auto()
    AUTO_CLEAN_SNAPSHOTS_CHANGED = auto()
    NAVIGATION_STYLE_CHANGED = auto()
    PAUSE_QUEUE_ON_FAILURE_CHANGED = auto()

@final
class NavigationStyle(Enum):
    """How views like wizards and installations are shown."""
    MODAL = "modal" # In separate windows over main window.
    PUSH = "push"   # Pushed in main window, closing them goes back.

@final
class Settings(Serializable):
    """Global application settings."""

    def __init__(
        self,
        initial_setup_done: bool = False,
        keep_root_unlocked: bool = True,
        toolsets_location: str = library_path("Toolsets"),
        snapshots_location: str = library_path("Snapshots"),
        releng_location: str = library_path("Releng"),
        overlay_location: str = library_path("Overlays"),
        project_location: str = library_path("Projects"),
        builds_location: str = library_path("Builds"),
        cache_location: str = library_path("Cache"),
        temporary_location: str = library_path("Temporary"),
        packages_location: str = library_path("Packages"),
        auto_clean_snapshots: bool = False,
        navigation_style: NavigationStyle = NavigationStyle.PUSH,
        pause_queue_on_failure: bool = False,
        qemu_memory_gib: int = 4,
        qemu_cpus: int = 4,
    ):
        self._initial_setup_done = initial_setup_done
        self._keep_root_unlocked = keep_root_unlocked
        self._toolsets_location = toolsets_location
        self._snapshots_location = snapshots_location
        self._releng_location = releng_location
        self._overlay_location = overlay_location
        self._project_location = project_location
        self._builds_location = builds_location
        self._cache_location = cache_location # Reusable data, safe to delete (distfiles, extracted squashfs files).
        self._temporary_location = temporary_location # Data of running operations, emptied when app starts.
        self._packages_location = packages_location # Binary packages folders, shared by projects.
        self._auto_clean_snapshots = auto_clean_snapshots # Snapshots not used by projects are removed (except newest).
        self._navigation_style = navigation_style
        self._pause_queue_on_failure = pause_queue_on_failure # Queued builds wait when build fails.
        self._qemu_memory_gib = qemu_memory_gib
        self._qemu_cpus = qemu_cpus
        self.event_bus = EventBus[SettingsEvents]()

    @classmethod
    def init_from(cls, data: dict) -> Settings:
        try:
            return cls(
                initial_setup_done=data["initial_setup_done"],
                keep_root_unlocked=data["keep_root_unlocked"],
                toolsets_location=data["toolsets_location"],
                snapshots_location=data["snapshots_location"],
                releng_location=data["releng_location"],
                overlay_location=data["overlay_location"],
                project_location=data["project_location"],
                # Added later, settings stored by older versions don't contain it.
                builds_location=data.get("builds_location", library_path("Builds")),
                cache_location=data.get("cache_location", library_path("Cache")),
                temporary_location=data.get("temporary_location", library_path("Temporary")),
                packages_location=data.get("packages_location", library_path("Packages")),
                auto_clean_snapshots=data.get("auto_clean_snapshots", False),
                navigation_style=_navigation_style(data.get("navigation_style")),
                pause_queue_on_failure=data.get("pause_queue_on_failure", False),
                qemu_memory_gib=data.get("qemu_memory_gib", 4),
                qemu_cpus=data.get("qemu_cpus", 4),
            )
        except:
            return cls()

    def serialize(self) -> dict:
        return {
            "initial_setup_done": self.initial_setup_done,
            "keep_root_unlocked": self.keep_root_unlocked,
            "toolsets_location": self.toolsets_location,
            "snapshots_location": self.snapshots_location,
            "releng_location": self.releng_location,
            "overlay_location": self.overlay_location,
            "project_location": self.project_location,
            "builds_location": self.builds_location,
            "cache_location": self.cache_location,
            "temporary_location": self.temporary_location,
            "packages_location": self.packages_location,
            "auto_clean_snapshots": self.auto_clean_snapshots,
            "navigation_style": self.navigation_style.value,
            "pause_queue_on_failure": self.pause_queue_on_failure,
            "qemu_memory_gib": self.qemu_memory_gib,
            "qemu_cpus": self.qemu_cpus
        }

    # --------------------------------------------------------------------------
    # Accessors for keep_root_unlocked:

    @property
    def initial_setup_done(self) -> bool:
        return self._initial_setup_done
    @initial_setup_done.setter
    def initial_setup_done(self, value: bool):
        if self._initial_setup_done != value:
            self._initial_setup_done = value
            self.event_bus.emit(
                SettingsEvents.INITIAL_SETUP_DONE_CHANGED,
                value
            )
            Repository.Settings.save()

    # --------------------------------------------------------------------------
    # Accessors for keep_root_unlocked:

    @property
    def keep_root_unlocked(self) -> bool:
        return self._keep_root_unlocked
    @keep_root_unlocked.setter
    def keep_root_unlocked(self, value: bool):
        if self._keep_root_unlocked != value:
            self._keep_root_unlocked = value
            self.event_bus.emit(
                SettingsEvents.KEEP_ROOT_UNLOCKED_CHANGED,
                value
            )
            Repository.Settings.save()

    # --------------------------------------------------------------------------
    # Accessors for toolsets location:

    @property
    def toolsets_location(self) -> bool:
        return self._toolsets_location
    @toolsets_location.setter
    def toolsets_location(self, value: bool):
        if self._toolsets_location != value:
            self._toolsets_location = value
            self.event_bus.emit(
                SettingsEvents.TOOLSETS_LOCATION_CHANGED,
                value
            )
            Repository.Settings.save()

    # --------------------------------------------------------------------------
    # Accessors for snapshots location:

    @property
    def snapshots_location(self) -> bool:
        return self._snapshots_location
    @snapshots_location.setter
    def snapshots_location(self, value: bool):
        if self._snapshots_location != value:
            self._snapshots_location = value
            self.event_bus.emit(
                SettingsEvents.SNAPSHOTS_LOCATION_CHANGED,
                value
            )
            Repository.Settings.save()

    # --------------------------------------------------------------------------
    # Accessors for releng location:

    @property
    def releng_location(self) -> bool:
        return self._releng_location
    @releng_location.setter
    def releng_location(self, value: bool):
        if self._releng_location != value:
            self._releng_location = value
            self.event_bus.emit(
                SettingsEvents.RELENG_LOCATION_CHANGED,
                value
            )
            Repository.Settings.save()

    # --------------------------------------------------------------------------
    # Accessors for overlay location:

    @property
    def overlay_location(self) -> bool:
        return self._overlay_location
    @overlay_location.setter
    def overlay_location(self, value: bool):
        if self._overlay_location != value:
            self._overlay_location = value
            self.event_bus.emit(
                SettingsEvents.OVERLAY_LOCATION_CHANGED,
                value
            )
            Repository.Settings.save()

    # --------------------------------------------------------------------------
    # Accessors for project location:

    @property
    def project_location(self) -> bool:
        return self._project_location
    @project_location.setter
    def project_location(self, value: bool):
        if self._project_location != value:
            self._project_location = value
            self.event_bus.emit(
                SettingsEvents.PROJECT_LOCATION_CHANGED,
                value
            )
            Repository.Settings.save()


    # --------------------------------------------------------------------------
    # Accessors for builds location:

    @property
    def builds_location(self) -> str:
        return self._builds_location
    @builds_location.setter
    def builds_location(self, value: str):
        if self._builds_location != value:
            self._builds_location = value
            self.event_bus.emit(
                SettingsEvents.BUILDS_LOCATION_CHANGED,
                value
            )
            Repository.Settings.save()

    # --------------------------------------------------------------------------
    # Accessors for cache location:

    @property
    def cache_location(self) -> str:
        return self._cache_location
    @cache_location.setter
    def cache_location(self, value: str):
        if self._cache_location != value:
            self._cache_location = value
            self.event_bus.emit(
                SettingsEvents.CACHE_LOCATION_CHANGED,
                value
            )
            Repository.Settings.save()

    # --------------------------------------------------------------------------
    # Accessors for temporary location:

    @property
    def temporary_location(self) -> str:
        return self._temporary_location
    @temporary_location.setter
    def temporary_location(self, value: str):
        if self._temporary_location != value:
            self._temporary_location = value
            self.event_bus.emit(
                SettingsEvents.TEMPORARY_LOCATION_CHANGED,
                value
            )
            Repository.Settings.save()

    # --------------------------------------------------------------------------
    # Accessors for packages location:

    @property
    def packages_location(self) -> str:
        return self._packages_location
    @packages_location.setter
    def packages_location(self, value: str):
        if self._packages_location != value:
            self._packages_location = value
            self.event_bus.emit(
                SettingsEvents.PACKAGES_LOCATION_CHANGED,
                value
            )
            Repository.Settings.save()

    # --------------------------------------------------------------------------
    # Accessors for auto clean snapshots:

    @property
    def auto_clean_snapshots(self) -> bool:
        return self._auto_clean_snapshots
    @auto_clean_snapshots.setter
    def auto_clean_snapshots(self, value: bool):
        if self._auto_clean_snapshots != value:
            self._auto_clean_snapshots = value
            self.event_bus.emit(
                SettingsEvents.AUTO_CLEAN_SNAPSHOTS_CHANGED,
                value
            )
            Repository.Settings.save()

    # --------------------------------------------------------------------------
    # Accessors for navigation style:

    @property
    def navigation_style(self) -> NavigationStyle:
        return self._navigation_style
    @navigation_style.setter
    def navigation_style(self, value: NavigationStyle):
        if self._navigation_style != value:
            self._navigation_style = value
            self.event_bus.emit(
                SettingsEvents.NAVIGATION_STYLE_CHANGED,
                value
            )
            Repository.Settings.save()

    # --------------------------------------------------------------------------
    # Accessors for pausing build queue on failure:

    @property
    def pause_queue_on_failure(self) -> bool:
        return self._pause_queue_on_failure
    @pause_queue_on_failure.setter
    def pause_queue_on_failure(self, value: bool):
        if self._pause_queue_on_failure != value:
            self._pause_queue_on_failure = value
            self.event_bus.emit(
                SettingsEvents.PAUSE_QUEUE_ON_FAILURE_CHANGED,
                value
            )
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
    except ValueError:
        return NavigationStyle.PUSH
