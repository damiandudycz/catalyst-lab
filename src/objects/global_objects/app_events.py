from __future__ import annotations
from enum import Enum, auto
from typing import final
from .event_bus import EventBus

@final
class AppEvents(Enum):
    OPEN_APP_SECTION = auto() # Args: (section: AppSection)
    PUSH_VIEW = auto() # Push on Main Navigation View (Full window mode). Args: (view: Gtk.Widget), kwargs: (title=<title>).
    PUSH_SECTION = auto() # Like PUSH_VIEW but for pushing by AppSection enum. # Args: (section: AppSection)
    PRESENT_VIEW = auto() # Present as dialog over app window.
    PRESENT_SECTION = auto()
    TOGGLE_SIDEBAR = auto

app_event_bus = EventBus[AppEvents]()
