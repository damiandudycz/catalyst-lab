from typing import final
from .git_update import GitUpdate
from .project_manager import ProjectManager

@final
class ProjectUpdate(GitUpdate):
    """Handles the full project directory update lifecycle."""

    manager_type = ProjectManager

