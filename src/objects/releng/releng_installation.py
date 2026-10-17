from typing import final
from .git_installation import GitInstallation
from .releng_manager import RelengManager

@final
class RelengInstallation(GitInstallation):
    """Handles the full releng directory installation lifecycle."""

    manager_type = RelengManager

