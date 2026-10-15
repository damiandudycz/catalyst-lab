from typing import final
from .git_update import GitUpdate
from .git_manager import GitManager
from .releng_manager import RelengManager

@final
class RelengUpdate(GitUpdate):
    """Handles the full releng directory update lifecycle."""

    # Overwrite in subclassed
    @classmethod
    def manager(cls) -> GitManager:
        return RelengManager.shared()

