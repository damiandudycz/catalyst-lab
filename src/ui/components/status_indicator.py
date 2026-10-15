import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, cairo, GLib
import math
from enum import Enum, auto
from collections import namedtuple

# Item can have many states at once (eg. running and has changes). Indicator shows the most important one, details
# list all of them with their own states (shown when indicator is hovered).
StatusDetail = namedtuple("StatusDetail", ["state", "text"])
StatusIndicatorValues = namedtuple("StatusIndicatorValues", ["state", "blinking", "details"], defaults=((),))

class StatusIndicatorState(Enum):
    """States of items shown with color of indicator, from least to most important (item with many states shows the
    most important one). Item that is actively used (eg. building, running operation) blinks. Described in README."""
    IDLE = auto()     # Gray: not used, not mounted, no changes.
    SUCCEEDED = auto() # Green: last operation completed (eg. build).
    LOADED = auto()   # Blue: mounted, loaded or running.
    CHANGED = auto()  # Purple: has changes (eg. not saved changes of Git directory).
    WARNING = auto()  # Orange: has warnings.
    ERROR = auto()    # Red: has errors.

    def color(self) -> tuple[float, float, float]:
        """Colors of GNOME palette (gray is light 5, others are 3), the same in light and dark style."""
        match self:
            case StatusIndicatorState.IDLE: return (0x9a / 255, 0x99 / 255, 0x96 / 255)
            case StatusIndicatorState.SUCCEEDED: return (0x33 / 255, 0xd1 / 255, 0x7a / 255)
            case StatusIndicatorState.LOADED: return (0x35 / 255, 0x84 / 255, 0xe4 / 255)
            case StatusIndicatorState.CHANGED: return (0x91 / 255, 0x41 / 255, 0xac / 255)
            case StatusIndicatorState.WARNING: return (0xff / 255, 0x78 / 255, 0x00 / 255)
            case StatusIndicatorState.ERROR: return (0xe0 / 255, 0x1b / 255, 0x24 / 255)

def status_values(state: StatusIndicatorState, blinking: bool = False, description: str | None = None) -> StatusIndicatorValues:
    """Indicator values of item with single state. Actively used item is at least loaded, so blinking is visible."""
    return item_status([StatusDetail(state, description)] if description else [], blinking=blinking, state=state)

def item_status(details: list[StatusDetail | None], blinking: bool = False,
                state: StatusIndicatorState | None = None) -> StatusIndicatorValues:
    """Indicator values of item with states in details (none are skipped). Indicator shows the most important of them
    (or given state), actively used item is at least loaded, so blinking is visible."""
    # Most important first, like the state shown by indicator.
    details = tuple(sorted((detail for detail in details if detail is not None and detail.text),
                           key=lambda detail: detail.state.value, reverse=True))
    if state is None:
        state = most_important_state(*(detail.state for detail in details)) if details else StatusIndicatorState.IDLE
    if blinking and state == StatusIndicatorState.IDLE:
        state = StatusIndicatorState.LOADED
    return StatusIndicatorValues(state=state, blinking=blinking, details=details)

def most_important_state(*states: StatusIndicatorState) -> StatusIndicatorState:
    return max(states, key=lambda state: state.value)

class StatusIndicator(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        # Set fixed content size
        self.set_content_width(10)
        self.set_content_height(10)
        # State
        self._state = StatusIndicatorState.IDLE
        self._blinking = False
        self._tick_id = None
        self._dimmed = False
        # Use custom draw function
        self.set_draw_func(self._on_draw)
        # Tooltip lists states with their own indicators.
        self._details: tuple = ()
        self.set_has_tooltip(False)
        self.connect("query-tooltip", self._on_query_tooltip)

    def set_values(self, values: StatusIndicatorValues):
        values = item_status(list(values.details), values.blinking, values.state)
        self.set_state(values.state)
        self.set_blinking(values.blinking)
        self._details = values.details
        self.set_has_tooltip(bool(values.details))

    def _on_query_tooltip(self, widget, x, y, keyboard_mode, tooltip):
        if not self._details:
            return False
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for detail in self._details:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            indicator = StatusIndicator()
            indicator.set_state(detail.state)
            indicator.set_valign(Gtk.Align.CENTER)
            row.append(indicator)
            row.append(Gtk.Label(label=detail.text, xalign=0))
            box.append(row)
        tooltip.set_custom(box)
        return True

    def set_state(self, state: StatusIndicatorState):
        self._state = state
        self.queue_draw()

    def set_blinking(self, blinking: bool):
        """Enable or disable blinking animation."""
        if blinking == self._blinking:
            return
        self._blinking = blinking
        if blinking:
            self._tick_id = GLib.timeout_add(500, self._tick)
        else:
            if self._tick_id:
                GLib.source_remove(self._tick_id)
                self._tick_id = None
            self._dimmed = False
            self.queue_draw()

    def _tick(self):
        if not self._blinking:
            return False
        self._dimmed = not self._dimmed
        self.queue_draw()
        return True

    def _on_draw(self, area, ctx: cairo.Context, width, height):
        red, green, blue = self._state.color()
        alpha = 0.3 if self._dimmed else 1.0
        radius = min(width, height) / 2 - 0.5
        ctx.arc(width / 2, height / 2, radius, 0, 2 * math.pi)
        ctx.set_source_rgba(red, green, blue, alpha)
        ctx.fill_preserve()
        # Edge in darker shade of the same color.
        ctx.set_source_rgba(red * 0.7, green * 0.7, blue * 0.7, alpha)
        ctx.set_line_width(1)
        ctx.stroke()


# ------------------------------------------------------------------------------
# Status of whole section (eg. in side menu): statuses of its items and running operations, with keys identifying them
# (so side menu can hide states that were already seen). Combined status is the most important state of items, blinking
# when any item blinks or any of its operations runs.

StatusEntry = namedtuple("StatusEntry", ["key", "values", "label"], defaults=(None,)) # Label is name of item.

def combined_status(entries: list) -> StatusIndicatorValues | None:
    """Most important state of entries (StatusEntry or StatusIndicatorValues), None when there is nothing to show.
    Details list states of entries, with names of their items."""
    values = [entry.values if isinstance(entry, StatusEntry) else entry for entry in entries]
    values = [value for value in values if value is not None]
    if not values:
        return None
    state = most_important_state(*(value.state for value in values))
    blinking = any(value.blinking for value in values)
    if state == StatusIndicatorState.IDLE and not blinking:
        return None
    details = [detail for entry in entries for detail in entry_details(entry)]
    return item_status(details, blinking, state)

def entry_details(entry) -> list[StatusDetail]:
    """States of entry that are not idle, with name of its item (eg. "Raspberry Pi 5: Not saved changes")."""
    values = entry.values if isinstance(entry, StatusEntry) else entry
    if values is None:
        return []
    label = entry.label if isinstance(entry, StatusEntry) else None
    return [StatusDetail(detail.state, f"{label}: {detail.text}" if label else detail.text)
            for detail in values.details if detail.state != StatusIndicatorState.IDLE]

def items_status(items, property_name: str = "status_indicator_values") -> list[StatusEntry]:
    result = []
    for item in items:
        try:
            key = (property_name, getattr(item, "id", None) or getattr(item, "uuid", None) or id(item))
            result.append(StatusEntry(key=key, values=getattr(item, property_name), label=getattr(item, "name", None)))
        except Exception as e:
            print(f"Failed to read status of {item}: {e}")
    return result

def process_result_status(process) -> StatusIndicatorValues | None:
    """Running process blinks, finished one shows its result (completed, failed) until it's seen."""
    from .multistage_process import MultiStageProcessState
    if process.status == MultiStageProcessState.IN_PROGRESS:
        return status_values(StatusIndicatorState.LOADED, blinking=True, description=process.title)
    if getattr(process, "result_seen", False):
        return None
    if process.status == MultiStageProcessState.COMPLETED:
        return status_values(StatusIndicatorState.SUCCEEDED, description=f"{process.title} completed")
    if process.status == MultiStageProcessState.FAILED:
        return status_values(StatusIndicatorState.ERROR, description=f"{process.title} failed")
    return None

def processes_status(*process_classes) -> list[StatusEntry]:
    """Processes (installations, updates, builds, deploys) of given classes: running ones as blinking indicators,
    finished ones with their result until it's seen."""
    from .multistage_process import MultiStageProcess
    processes = list(dict.fromkeys(MultiStageProcess.started_processes + MultiStageProcess.finished_processes))
    entries = []
    for process in processes:
        if not isinstance(process, process_classes):
            continue
        if (values := process_result_status(process)) is not None:
            entries.append(StatusEntry(key=("process", id(process)), label=process.name(), values=values))
    return entries

# Results (eg. completed or failed build), warnings and errors are reported in side menu until their section is opened.
_HIDDEN_WHEN_SEEN = {StatusIndicatorState.SUCCEEDED, StatusIndicatorState.WARNING, StatusIndicatorState.ERROR}

def unseen_status_entries(entries: list[StatusEntry], seen: set, section_open: bool) -> tuple[list[StatusEntry], set]:
    """Entries of section shown in side menu, and updated seen states. Results, warnings and errors are shown until
    section is opened (they are seen while it's open). State that disappears and comes back (eg. result of next build)
    is shown again. Other states (changes, loaded, running operations) are always shown."""
    current = {(entry.key, entry.values.state) for entry in entries
               if entry.values is not None and entry.values.state in _HIDDEN_WHEN_SEEN}
    seen = current if section_open else seen & current
    unseen = [entry for entry in entries if entry.values is not None
              and (entry.values.blinking or (entry.key, entry.values.state) not in seen)]
    return unseen, seen
