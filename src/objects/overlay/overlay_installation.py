from typing import final
from .git_installation import GitInstallation
from .overlay_manager import OverlayManager

@final
class OverlayInstallation(GitInstallation):
    """Handles the full overlay directory installation lifecycle."""

    manager_type = OverlayManager

