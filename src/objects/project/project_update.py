from typing import final
from .git_update import GitUpdate
from .git_manager import GitManager
from .project_manager import ProjectManager

@final
class ProjectUpdate(GitUpdate):
    """Handles the full project directory update lifecycle."""

    # Overwrite in subclassed
    @classmethod
    def manager(cls) -> GitManager:
        return ProjectManager.shared()

