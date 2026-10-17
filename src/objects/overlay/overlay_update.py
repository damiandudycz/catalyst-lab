from typing import final
from .git_update import GitUpdate
from .overlay_manager import OverlayManager

@final
class OverlayUpdate(GitUpdate):
    """Handles the full overlay directory update lifecycle."""

    manager_type = OverlayManager

