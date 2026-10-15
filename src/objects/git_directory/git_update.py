from __future__ import annotations
import os, subprocess
from .multistage_process import (
    MultiStageProcess, MultiStageProcessStage,
    MultiStageProcessState, MultiStageProcessStageState
)
from .git_directory import GitDirectory
from .git_manager import GitManager
from abc import ABC, abstractmethod

# ------------------------------------------------------------------------------
# Git update.
# ------------------------------------------------------------------------------

class GitUpdate(MultiStageProcess, ABC):
    """Handles the Git directory update lifecycle."""

    # Overwrite in subclassed
    @classmethod
    @abstractmethod
    def manager(cls) -> GitManager:
        pass

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
        super().setup_stages()

    def complete_process(self, success: bool):
        if success:
            self.__class__.manager().repository().save()

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
    def run_git_command(self, repo_path: str, args):
        self.log(f"$ git {' '.join(args)}")
        result = subprocess.run(
            ["git"] + args,
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        for line in (result.stdout + result.stderr).splitlines():
            self.log(line)
        result.check_returncode()
        return result.stdout.strip()
    def start(self):
        super().start()
        try:
            self.process_started = True
            update_git_directory(self.directory, self.log)
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during Git directory update: {e}")
            self.complete(MultiStageProcessStageState.FAILED)
    def cleanup(self) -> bool:
        if not super().cleanup():
            return False
        if self.multistage_process.status == MultiStageProcessState.FAILED and self.process_started:
            self.run_git_command(self.directory.directory_path(), ["rebase", "--abort"])
        return True

def update_git_directory(directory: GitDirectory, log):
    """Fetches latest changes of Git directory and rebases its commits on top of them (like update process), outside
    of process (eg. overlays updated with project). Raises when update fails, rebase is aborted then."""
    path = directory.directory_path()
    def git(*arguments):
        log(f"$ git {' '.join(arguments)}")
        result = subprocess.run(["git", *arguments], cwd=path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        for line in result.stdout.splitlines():
            log(line)
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
