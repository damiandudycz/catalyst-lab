from collections import namedtuple
from enum import Enum, auto


StatusDetail = namedtuple("StatusDetail", ["state", "text"])
StatusIndicatorValues = namedtuple("StatusIndicatorValues", ["state", "blinking", "details"], defaults=((),))


class StatusIndicatorState(Enum):
    """Status severity and color used for library items and operations."""
    IDLE = auto()
    SUCCEEDED = auto()
    LOADED = auto()
    CHANGED = auto()
    WARNING = auto()
    ERROR = auto()

    def color(self) -> tuple[float, float, float]:
        channels = {
            StatusIndicatorState.IDLE: (0x9a, 0x99, 0x96),
            StatusIndicatorState.SUCCEEDED: (0x33, 0xd1, 0x7a),
            StatusIndicatorState.LOADED: (0x35, 0x84, 0xe4),
            StatusIndicatorState.CHANGED: (0x91, 0x41, 0xac),
            StatusIndicatorState.WARNING: (0xff, 0x78, 0x00),
            StatusIndicatorState.ERROR: (0xe0, 0x1b, 0x24),
        }[self]
        return tuple(channel / 255 for channel in channels)


def status_values(state: StatusIndicatorState, blinking: bool = False,
                  description: str | None = None) -> StatusIndicatorValues:
    return item_status([StatusDetail(state, description)] if description else [], blinking=blinking, state=state)


def item_status(details: list[StatusDetail | None], blinking: bool = False,
                state: StatusIndicatorState | None = None) -> StatusIndicatorValues:
    details = tuple(sorted((detail for detail in details if detail is not None and detail.text),
                           key=lambda detail: detail.state.value, reverse=True))
    if state is None:
        state = most_important_state(*(detail.state for detail in details)) if details else StatusIndicatorState.IDLE
    if blinking and state == StatusIndicatorState.IDLE:
        state = StatusIndicatorState.LOADED
    return StatusIndicatorValues(state=state, blinking=blinking, details=details)


def most_important_state(*states: StatusIndicatorState) -> StatusIndicatorState:
    return max(states, key=lambda state: state.value)


def process_result_status(process) -> StatusIndicatorValues | None:
    """Status shown for a running operation or its unseen result."""
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
