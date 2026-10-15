from typing import final
from .git_manager import GitManager
from .repository import Repository

@final
class RelengManager(GitManager):

    @classmethod
    def repository(cls) -> Repository:
        return Repository.RelengDirectory

