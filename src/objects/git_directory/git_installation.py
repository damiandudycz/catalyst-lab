from gi.repository import Gio
from abc import ABC, abstractmethod
from enum import Enum
from collections import namedtuple
from .multistage_process import (
    MultiStageProcess, MultiStageProcessStage,
    MultiStageProcessState, MultiStageProcessStageState
)
from .git_directory import GitDirectory
from .git_manager import GitManager
from .git_directory_default_content_builder import DefaultDirContentBuilder
import subprocess, re, os, shutil

# ------------------------------------------------------------------------------
# Helper objects.
# ------------------------------------------------------------------------------

class GitDirectorySource(Enum):
    GIT_REPOSITORY = 0 # Clone git repository.
    LOCAL_DIRECTORY = 1 # Copy local directory.
    CREATE_NEW = 2 # Create new empty directory.
    TEMPLATE = 3 # Create from template, data has repository_url of template (cloned) or None (new empty directory).

    def name(self) -> str:
        match self:
            case GitDirectorySource.GIT_REPOSITORY:
                return "Git repository"
            case GitDirectorySource.LOCAL_DIRECTORY:
                return "Local directory"
            case GitDirectorySource.CREATE_NEW:
                return "Create new"
            case GitDirectorySource.TEMPLATE:
                return "From template"

GitDirectorySetupConfiguration = namedtuple(
    "GitDirectorySetupConfiguration",
    ["source", "name", "data"]
)

# ------------------------------------------------------------------------------
# Git installation.
# ------------------------------------------------------------------------------

class GitInstallation(MultiStageProcess, ABC):
    """Handles the full git directory installation lifecycle."""

    # Overwrite in subclassed
    @classmethod
    @abstractmethod
    def manager(cls) -> GitManager:
        pass

    def __init__(self, configuration: GitDirectorySetupConfiguration):
        self.configuration = configuration
        super().__init__(title="Git directory installation")

    def name(self) -> str:
        return self.configuration.name

    def setup_stages(self):
        match self.configuration.source:
            case GitDirectorySource.GIT_REPOSITORY:
                self.stages.append(
                    GitInstallationStepClone(
                        dir_name=self.configuration.name,
                        repository_url=self.configuration.data,
                        item_class=self.manager().repository()._cls,
                        multistage_process=self
                    )
                )
            case GitDirectorySource.LOCAL_DIRECTORY:
                self.stages.append(
                    GitInstallationStepCopyLocal(
                        dir_name=self.configuration.name,
                        local_path=self.configuration.data,
                        item_class=self.manager().repository()._cls,
                        multistage_process=self
                    )
                )
            case GitDirectorySource.CREATE_NEW:
                self.stages.append(
                    GitInstallationStepInitLocal(
                        dir_name=self.configuration.name,
                        default_dir_content_builder=self.configuration.data,
                        item_class=self.manager().repository()._cls,
                        multistage_process=self
                    )
                )
            case GitDirectorySource.TEMPLATE:
                # Content is created from template by subclass, template repository is cloned like Git repository.
                if self.configuration.data.repository_url:
                    self.stages.append(
                        GitInstallationStepClone(
                            dir_name=self.configuration.name,
                            repository_url=self.configuration.data.repository_url,
                            item_class=self.manager().repository()._cls,
                            multistage_process=self
                        )
                    )
                else:
                    self.stages.append(
                        GitInstallationStepInitLocal(
                            dir_name=self.configuration.name,
                            default_dir_content_builder=None,
                            item_class=self.manager().repository()._cls,
                            multistage_process=self
                        )
                    )
        self.stages.append(
            GitInstallationStepSetupRepository(multistage_process=self)
        )
        self.stages.append(
            GitInstallationStepAnalyzeRepository(multistage_process=self)
        )

    def complete_process(self, success: bool):
        if success:
            manager = self.__class__.manager()
            manager.add_directory(directory=self.directory)

# ------------------------------------------------------------------------------
# Installation process steps.
# ------------------------------------------------------------------------------

class GitInstallationStepClone(MultiStageProcessStage):
    def __init__(
        self,
        dir_name: str,
        repository_url: str,
        item_class: type[GitDirectory],
        multistage_process: MultiStageProcess
    ):
        super().__init__(
            name="Clone Git repository",
            description="Clones Git repository",
            multistage_process=multistage_process
        )
        self.dir_name = dir_name
        self.repository_url = repository_url
        self.item_class = item_class
        self.process_started = False
    def start(self):
        super().start()
        try:
            self.directory = self.item_class(name=self.dir_name)
            path=self.directory.directory_path()
            if os.path.exists(path):
                raise RuntimeError(f"Directory {path} already exists")
            self.process_started = True
            progress_pattern = re.compile(r"Receiving objects:\s+(\d+)%")
            process = subprocess.Popen(
                [
                    "git",
                    "clone",
                    "--progress",
                    self.repository_url,
                    self.directory.directory_path()
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1
            )
            for line in process.stdout:
                match = progress_pattern.search(line)
                if match:
                    percent = int(match.group(1))
                    self._update_progress(percent / 100)
                elif "%" not in line: # Skip repeated progress lines
                    self.log(line.rstrip())
            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(
                    process.returncode,
                    process.args
                )
            self.multistage_process.directory = self.directory
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during releng cloning: {e}")
            self.complete(MultiStageProcessStageState.FAILED)
    def cleanup(self) -> bool:
        if not super().cleanup():
            return False
        if self.multistage_process.status == MultiStageProcessState.FAILED and self.process_started:
            if os.path.exists(self.directory.directory_path()):
                shutil.rmtree(self.directory.directory_path())
        return True

class GitInstallationStepCopyLocal(MultiStageProcessStage):
    def __init__(
        self,
        dir_name: str,
        local_path: Gio.File,
        item_class: type[GitDirectory],
        multistage_process: MultiStageProcess
    ):
        super().__init__(
            name="Copy Local directory",
            description="Copies contents of local directory",
            multistage_process=multistage_process
        )
        self.dir_name = dir_name
        self.local_path = local_path
        self.item_class = item_class
        self.process_started = False
    def start(self):
        super().start()
        try:
            self.directory = self.item_class(name=self.dir_name)
            path = self.directory.directory_path()
            if os.path.exists(path):
                raise RuntimeError(f"Directory {path} already exists")
            if not os.path.isdir(self.local_path):
                raise RuntimeError(f"Path {self.local_path} is not a directory")
            self.process_started = True
            # Create destination directory:
            dest = Gio.File.new_for_path(path)
            dest.make_directory_with_parents(None)
            self.multistage_process.directory = self.directory
            # Calculating number of files
            def count_total_files(dir: Gio.File) -> int:
                """Recursively count the number of files (not directories) in a directory."""
                total = 0
                enumerator = dir.enumerate_children(
                    "standard::name,standard::type",
                    Gio.FileQueryInfoFlags.NONE,
                    None
                )
                info = enumerator.next_file(None)
                while info:
                    file_type = info.get_file_type()
                    name = info.get_name()
                    child = dir.get_child(name)
                    if file_type == Gio.FileType.DIRECTORY:
                        total += count_total_files(child)
                    else:
                        total += 1
                    info = enumerator.next_file(None)
                return total
            # Copy files:
            def copy_directory_contents_with_progress(
                source: Gio.File,
                destination_path: str
            ):
                dest = Gio.File.new_for_path(destination_path)
                if not dest.query_exists(None):
                    dest.make_directory_with_parents(None)
                total_files = count_total_files(source)
                copied_files = 0
                def copy_recursive(src: Gio.File, dst: Gio.File):
                    nonlocal copied_files
                    enumerator = src.enumerate_children(
                        "standard::name,standard::type",
                        Gio.FileQueryInfoFlags.NONE,
                        None
                    )
                    info = enumerator.next_file(None)
                    while info:
                        name = info.get_name()
                        file_type = info.get_file_type()
                        child_src = src.get_child(name)
                        child_dst = dst.get_child(name)
                        if file_type == Gio.FileType.DIRECTORY:
                            child_dst.make_directory_with_parents(None)
                            copy_recursive(child_src, child_dst)
                        else:
                            child_src.copy(
                                child_dst,
                                Gio.FileCopyFlags.OVERWRITE,
                                None, None, None
                            )
                            copied_files += 1
                            self._update_progress(copied_files / total_files)
                        info = enumerator.next_file(None)
                copy_recursive(source, dest)
            copy_directory_contents_with_progress(
                source=self.local_path,
                destination_path=path
            )
            # Complete
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during copying local directory: {e}")
            self.complete(MultiStageProcessStageState.FAILED)
    def cleanup(self) -> bool:
        if not super().cleanup():
            return False
        if self.multistage_process.status == MultiStageProcessState.FAILED and self.process_started:
            path = self.directory.directory_path()
            if os.path.exists(path):
                shutil.rmtree(path)
        return True

class GitInstallationStepInitLocal(MultiStageProcessStage):
    def __init__(
        self,
        dir_name: str,
        default_dir_content_builder: DefaultDirContentBuilder | None,
        item_class: type[GitDirectory],
        multistage_process: MultiStageProcess
    ):
        super().__init__(
            name="Create new directory",
            description="Creates new directory with default content",
            multistage_process=multistage_process
        )
        self.dir_name = dir_name
        self.default_dir_content_builder = default_dir_content_builder
        self.item_class = item_class
        self.process_started = False
    def start(self):
        super().start()
        try:
            self.directory = self.item_class(name=self.dir_name)
            path = self.directory.directory_path()
            if os.path.exists(path):
                raise RuntimeError(f"Directory {path} already exists")
            self.process_started = True
            os.makedirs(path, exist_ok=True)
            if self.default_dir_content_builder:
                self.default_dir_content_builder.build_in(path, self.dir_name)
            self.multistage_process.directory = self.directory
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during creating new directory: {e}")
            self.complete(MultiStageProcessStageState.FAILED)
    def cleanup(self) -> bool:
        if not super().cleanup():
            return False
        if self.multistage_process.status == MultiStageProcessState.FAILED and self.process_started:
            path = self.directory.directory_path()
            if os.path.exists(path):
                shutil.rmtree(path)
        return True

class GitInstallationStepSetupRepository(MultiStageProcessStage):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(
            name="Configure Git repository",
            description="Creates empty Git repository if not present",
            multistage_process=multistage_process
        )
    def run_logged_command(self, cmd: list[str]):
        """Runs command, adding its output to stage output."""
        self.log("$ " + " ".join(cmd))
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        for line in result.stdout.splitlines():
            self.log(line)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, result.args, output=result.stdout)
    def start(self):
        super().start()
        try:
            path = self.multistage_process.directory.directory_path()
            git_dir_path = os.path.join(path, '.git')
            if not os.path.exists(git_dir_path):
                # Initialize repo:
                commands = [
                    ["git", "init", "--initial-branch=main", path],
                    ["git", "-C", path, "add", "."]
                ]
                for cmd in commands:
                    self.run_logged_command(cmd)
                # Commit:
                if subprocess.run(
                    ["git", "-C", path, "status", "--porcelain"],
                    stdout=subprocess.PIPE,
                    text=True
                ).stdout.strip():
                    self.run_logged_command(["git", "-C", path, "commit", "-m", "Initial commit"])
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during local Git init: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

class GitInstallationStepAnalyzeRepository(MultiStageProcessStage):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(
            name="Analyze Git repository",
            description="Reads state and logs of git repository",
            multistage_process=multistage_process
        )
    def start(self):
        super().start()
        try:
            self.multistage_process.directory.update_status(wait=True)
            self.multistage_process.directory.update_logs(wait=True)
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during analyzing git repository: {e}")
            self.complete(MultiStageProcessStageState.FAILED)

