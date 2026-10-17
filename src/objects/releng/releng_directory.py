from typing import final
from .git_directory import GitDirectory

@final
class RelengDirectory(GitDirectory):

    @classmethod
    def base_location(cls) -> str:
        from .repository import Repository, resolve_path
        return resolve_path(Repository.Settings.value.releng_location)

