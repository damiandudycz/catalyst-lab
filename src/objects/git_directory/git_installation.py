from gi.repository import Gio
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

    def display_name(self) -> str:
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

class GitInstallation(MultiStageProcess):
    """Handles the full git directory installation lifecycle."""

    manager_type: type[GitManager]

    @classmethod
    def manager(cls) -> GitManager:
        return cls.manager_type.shared()

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

def _remove_partial_directory(step):
    if step.multistage_process.status == MultiStageProcessState.FAILED and step.process_started:
        path = step.directory.directory_path()
        if os.path.exists(path):
            shutil.rmtree(path)

def _directory_entries(directory: Gio.File, parent: str = ""):
    enumerator = directory.enumerate_children(
        "standard::name,standard::type", Gio.FileQueryInfoFlags.NONE, None
    )
    try:
        while info := enumerator.next_file(None):
            name = info.get_name()
            relative_path = os.path.join(parent, name)
            child = directory.get_child(name)
            is_directory = info.get_file_type() == Gio.FileType.DIRECTORY
            yield relative_path, is_directory
            if is_directory:
                yield from _directory_entries(child, relative_path)
    finally:
        enumerator.close(None)

def _copy_local_directory(source: Gio.File, destination: Gio.File, progress):
    total_files = sum(not is_directory for _, is_directory in _directory_entries(source))
    copied_files = 0
    for relative_path, is_directory in _directory_entries(source):
        target = destination.get_child(relative_path)
        if is_directory:
            target.make_directory_with_parents(None)
        else:
            source.get_child(relative_path).copy(target, Gio.FileCopyFlags.OVERWRITE, None, None, None)
            copied_files += 1
            progress(copied_files / total_files)

class _GitInstallationDirectoryStep(MultiStageProcessStage):
    def __init__(self, name: str, description: str, multistage_process: MultiStageProcess):
        super().__init__(name, description, multistage_process)
        self.process_started = False

    def create_directory(self, dir_name: str, item_class: type[GitDirectory], create, error_context: str):
        super().start()
        try:
            self.directory = item_class(name=dir_name)
            path = self.directory.directory_path()
            if os.path.exists(path):
                raise RuntimeError(f"Directory {path} already exists")
            create(path)
            self.multistage_process.directory = self.directory
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as error:
            print(f"{error_context}: {error}")
            self.complete(MultiStageProcessStageState.FAILED)

    def cleanup(self) -> bool:
        if not super().cleanup():
            return False
        _remove_partial_directory(self)
        return True

class GitInstallationStepClone(_GitInstallationDirectoryStep):
    def __init__(
        self,
        dir_name: str,
        repository_url: str,
        item_class: type[GitDirectory],
        multistage_process: MultiStageProcess
    ):
        super().__init__("Clone Git repository", "Clones Git repository", multistage_process)
        self.dir_name = dir_name
        self.repository_url = repository_url
        self.item_class = item_class
    def start(self):
        self.create_directory(self.dir_name, self.item_class, self._clone, "Error during releng cloning")

    def _clone(self, path: str):
        self.process_started = True
        progress_pattern = re.compile(r"Receiving objects:\s+(\d+)%")
        process = subprocess.Popen(
            ["git", "clone", "--progress", self.repository_url, path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        for line in process.stdout:
            if match := progress_pattern.search(line):
                self._update_progress(int(match.group(1)) / 100)
            elif "%" not in line: # Skip repeated progress lines
                self.log(line.rstrip())
        process.wait()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, process.args)
class GitInstallationStepCopyLocal(_GitInstallationDirectoryStep):
    def __init__(
        self,
        dir_name: str,
        local_path: Gio.File,
        item_class: type[GitDirectory],
        multistage_process: MultiStageProcess
    ):
        super().__init__("Copy Local directory", "Copies contents of local directory", multistage_process)
        self.dir_name = dir_name
        self.local_path = local_path
        self.item_class = item_class
    def start(self):
        self.create_directory(self.dir_name, self.item_class, self._copy, "Error during copying local directory")

    def _copy(self, path: str):
        source_path = self.local_path.get_path()
        if not source_path or not os.path.isdir(source_path):
            raise RuntimeError(f"Path {self.local_path} is not a directory")
        self.process_started = True
        destination = Gio.File.new_for_path(path)
        destination.make_directory_with_parents(None)
        _copy_local_directory(self.local_path, destination, self._update_progress)
class GitInstallationStepInitLocal(_GitInstallationDirectoryStep):
    def __init__(
        self,
        dir_name: str,
        default_dir_content_builder: DefaultDirContentBuilder | None,
        item_class: type[GitDirectory],
        multistage_process: MultiStageProcess
    ):
        super().__init__("Create new directory", "Creates new directory with default content", multistage_process)
        self.dir_name = dir_name
        self.default_dir_content_builder = default_dir_content_builder
        self.item_class = item_class
    def start(self):
        self.create_directory(self.dir_name, self.item_class, self._initialize, "Error during creating new directory")

    def _initialize(self, path: str):
        self.process_started = True
        os.makedirs(path, exist_ok=True)
        if self.default_dir_content_builder:
            self.default_dir_content_builder.build_in(path, self.dir_name)
class GitInstallationStepSetupRepository(MultiStageProcessStage):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(
            name="Configure Git repository",
            description="Creates empty Git repository if not present",
            multistage_process=multistage_process
        )
    def start(self):
        super().start()
        try:
            directory = self.multistage_process.directory
            path = directory.directory_path()
            result = directory._run_git("rev-parse", "--show-toplevel", stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True)
            is_repository = result.returncode == 0 and os.path.realpath(result.stdout.strip()) == os.path.realpath(path)
            if not is_repository:
                def git(*arguments):
                    result = directory.run_git_logged(arguments, self.log)
                    if result.returncode != 0:
                        raise subprocess.CalledProcessError(result.returncode, result.args, output=result.stdout)
                git("init", "--initial-branch=main")
                git("add", ".")
                if directory._run_git("status", "--porcelain", stdout=subprocess.PIPE, text=True).stdout.strip():
                    git("commit", "-m", "Initial commit")
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            self.log(f"Error: {e}")
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

