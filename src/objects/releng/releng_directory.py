from typing import final
from .git_directory import GitDirectory

@final
class RelengDirectory(GitDirectory):

    @classmethod
    def base_location(cls) -> str:
        from .repository import Repository
        import os
        return os.path.realpath(
            os.path.expanduser(
                Repository.Settings.value.releng_location
            )
        )

