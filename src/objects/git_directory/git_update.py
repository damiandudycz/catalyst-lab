from __future__ import annotations
import os, subprocess
from .multistage_process import (
    MultiStageProcess, MultiStageProcessStage,
    MultiStageProcessStageState
)
from .git_directory import GitDirectory
from .git_directory import GitDirectoryEvent
from .git_manager import GitManager

# ------------------------------------------------------------------------------
# Git update.
# ------------------------------------------------------------------------------

class GitUpdate(MultiStageProcess):
    """Handles the Git directory update lifecycle."""

    manager_type: type[GitManager]

    @classmethod
    def manager(cls) -> GitManager:
        return cls.manager_type.shared()

    def __init__(self, directory: GitDirectory):
        self.directory = directory
        super().__init__(title="Git directory update")

    def setup_stages(self):
        self.stages.append(
            GitUpdateStepUpdate(
                directory=self.directory,
                multistage_process=self
            )
        )
    def complete_process(self, success: bool):
        if success:
            self.__class__.manager().repository().save()
            self.directory.checkpoint_committed_contents()
            self.directory.reload_contents()
            self.directory.event_bus.emit(GitDirectoryEvent.CONTENT_RELOADED, self.directory)

# ------------------------------------------------------------------------------
# Update process steps.
# ------------------------------------------------------------------------------

class GitUpdateStepUpdate(MultiStageProcessStage):
    def __init__(
        self,
        directory: GitDirectory,
        multistage_process: MultiStageProcess
    ):
        super().__init__(
            name="Update Git directory",
            description="Fetch latest changes from git and rebase",
            multistage_process=multistage_process
        )
        self.directory = directory
    def start(self):
        super().start()
        try:
            update_git_directory(self.directory, self.log)
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during Git directory update: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

def update_git_directory(directory: GitDirectory, log):
    """Fetches latest changes of Git directory and rebases its commits on top of them (like update process), outside
    of process (eg. overlays updated with project). Raises when update fails, rebase is aborted then."""
    path = directory.directory_path()
    def git(*arguments):
        result = directory.run_git_logged(arguments, log, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if result.returncode != 0:
            raise RuntimeError(f"git {arguments[0]} failed in {directory.name}")
    with directory.operation():
        if not os.path.isdir(path):
            raise RuntimeError(f"Directory {path} does not exist for update")
        try:
            git("fetch", "--all", "--prune")
            git("rebase", "--autostash", "--rebase-merges", "origin/HEAD")
        except Exception:
            subprocess.run(["git", "rebase", "--abort"], cwd=path, capture_output=True)
            raise
        finally:
            directory.update_status(wait=True)
            directory.update_logs(wait=True)
