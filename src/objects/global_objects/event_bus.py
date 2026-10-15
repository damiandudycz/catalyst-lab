import weakref
from enum import Enum, auto
from typing import final, TypeVar, Generic, Callable, Dict, Any, Hashable
from gi.repository import GLib

EventBusType = TypeVar("EventBusType", bound=Enum)

class SharedEvent(Enum):
    STATE_UPDATED = auto()

@final
class EventBus(Generic[EventBusType]):
    def __init__(self, scheduler: Callable[..., Any] = GLib.idle_add):
        self.scheduler = scheduler
        self._subscribers: Dict[EventBusType, list[weakref.ReferenceType | weakref.WeakMethod]] = {}
        self._handles: Dict[EventBusType, Dict[Hashable, weakref.ReferenceType | weakref.WeakMethod]] = {}

    def subscribe(self, event: EventBusType, callback: Callable, handle: Hashable | None = None):
        if hasattr(callback, '__self__') and callback.__self__ is not None:
            ref = weakref.WeakMethod(callback)
        else:
            ref = weakref.ref(callback)
        if handle is not None:
            if event not in self._handles:
                self._handles[event] = {}
            self._handles[event][handle] = ref
        if event not in self._subscribers:
            self._subscribers[event] = []
        self._subscribers[event].append(ref)

    def unsubscribe(self, event: EventBusType, handle: Hashable):
        """Unsubscribe a callback using its handle."""
        if event not in self._handles:
            return
        ref = self._handles[event].pop(handle, None)
        if ref and event in self._subscribers:
            try:
                self._subscribers[event].remove(ref)
            except ValueError:
                pass  # already removed or not found

    def emit(self, event: EventBusType, *args, **kwargs):
        callbacks = self._subscribers.get(event, [])
        alive_callbacks = []
        for ref in callbacks:
            callback = ref()
            if callback is not None:
                alive_callbacks.append(ref)
                self.scheduler(callback, *args, **kwargs)
        self._subscribers[event] = alive_callbacks
        # Also clean up any dead handles
        if event in self._handles:
            dead_handles = [h for h, r in self._handles[event].items() if r() is None]
            for h in dead_handles:
                del self._handles[event][h]

