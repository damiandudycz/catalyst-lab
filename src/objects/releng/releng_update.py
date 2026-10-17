from typing import final
from .git_update import GitUpdate
from .releng_manager import RelengManager

@final
class RelengUpdate(GitUpdate):
    """Handles the full releng directory update lifecycle."""

    manager_type = RelengManager

