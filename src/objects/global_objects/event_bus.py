import weakref
from enum import Enum, auto
from typing import final, TypeVar, Generic, Callable, Any, Hashable
from gi.repository import GLib

EventBusType = TypeVar("EventBusType", bound=Enum)

class SharedEvent(Enum):
    STATE_UPDATED = auto()

@final
class EventBus(Generic[EventBusType]):
    def __init__(self, scheduler: Callable[..., Any] = GLib.idle_add):
        self.scheduler = scheduler
        self._subscribers: dict[EventBusType, list[weakref.ReferenceType | weakref.WeakMethod]] = {}
        self._handles: dict[EventBusType, dict[Hashable, weakref.ReferenceType | weakref.WeakMethod]] = {}

    def subscribe(self, event: EventBusType, callback: Callable, handle: Hashable | None = None):
        ref = (weakref.WeakMethod(callback) if getattr(callback, '__self__', None) is not None
               else weakref.ref(callback))
        if handle is not None:
            self.unsubscribe(event, handle)
            self._handles.setdefault(event, {})[handle] = ref
        self._subscribers.setdefault(event, []).append(ref)

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
        for ref in tuple(self._subscribers.get(event, ())):
            callback = ref()
            if callback is not None:
                self.scheduler(callback, *args, **kwargs)
        if event in self._subscribers:
            self._subscribers[event][:] = [ref for ref in self._subscribers[event] if ref() is not None]
        # Also clean up any dead handles
        if event in self._handles:
            dead_handles = [h for h, r in self._handles[event].items() if r() is None]
            for h in dead_handles:
                del self._handles[event][h]

