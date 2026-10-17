import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, cairo, GLib
import math
from collections import namedtuple
from .status import (
    StatusDetail, StatusIndicatorState, StatusIndicatorValues, item_status,
    most_important_state, process_result_status,
)

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
        self.connect("realize", self._start_blinking)
        self.connect("unrealize", self._stop_blinking)

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
            self._start_blinking()
        else:
            self._stop_blinking()
            self._dimmed = False
            self.queue_draw()

    def _start_blinking(self, *_):
        if self._blinking and self._tick_id is None and self.get_realized():
            self._tick_id = GLib.timeout_add(500, self._tick)

    def _stop_blinking(self, *_):
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None

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
