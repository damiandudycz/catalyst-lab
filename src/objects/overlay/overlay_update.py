from typing import final
from .git_update import GitUpdate
from .git_manager import GitManager
from .overlay_manager import OverlayManager

@final
class OverlayUpdate(GitUpdate):
    """Handles the full overlay directory update lifecycle."""

    # Overwrite in subclassed
    @classmethod
    def manager(cls) -> GitManager:
        return OverlayManager.shared()

