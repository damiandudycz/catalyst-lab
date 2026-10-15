from typing import final
import subprocess
from .git_installation import GitInstallation, GitDirectorySetupConfiguration, GitDirectorySource
from .git_manager import GitManager
from .project_manager import ProjectManager
from .toolset import Toolset
from .releng_directory import RelengDirectory
from .snapshot import Snapshot, LATEST_SNAPSHOT
from .project_directory import ProjectConfiguration
from .architecture import Architecture
from .project_template import apply_project_template, load_cloned_template, ensure_template_overlays
from .project_template_update import record_template_state, repository_commit, template_tree
import os, shutil
from .multistage_process import (
    MultiStageProcess, MultiStageProcessStage,
    MultiStageProcessState, MultiStageProcessStageState
)

@final
class ProjectInstallation(GitInstallation):
    """Handles the full project directory installation lifecycle."""

    def __init__(
        self,
        source_config: GitDirectorySetupConfiguration,
        toolset: Toolset,
        releng_directory: RelengDirectory,
        snapshot: Snapshot,
        architecture: Architecture,
        packages_directory=None
    ):
        self.packages_directory = packages_directory # Existing binary packages folder, None creates new one.
        self.toolset = toolset
        self.releng_directory = releng_directory
        # Latest snapshot is only remembered, builds get it before building.
        self.latest_snapshot = snapshot is LATEST_SNAPSHOT
        self.snapshot = None if self.latest_snapshot else snapshot
        self.architecture = architecture
        super().__init__(configuration=source_config)

    # Overwrite in subclassed
    @classmethod
    def manager(cls) -> GitManager:
        return ProjectManager.shared()

    def complete_process(self, success: bool):
        if success:
            # Binary packages folder: selected one, or new one for project (CPU flags of its stages are known now).
            try:
                from .packages_directory import create_packages_directory_for_project
                directory = self.packages_directory or create_packages_directory_for_project(self.directory)
                self.directory.initialize_metadata().packages_directory_id = directory.id
            except Exception as e:
                print(f"Failed to set binary packages folder: {e}")
        super().complete_process(success)

    def setup_stages(self):
        super().setup_stages()
        save_config = ProjectInstallationStepSaveConfig(
            multistage_process=self,
            toolset=self.toolset,
            releng_directory=self.releng_directory,
            architecture=self.architecture
        )
        if self.configuration.source == GitDirectorySource.TEMPLATE:
            # Stages of template get default values from configuration, so it's saved before. Content is created before
            # Git repository is configured, so new repository has it in first commit.
            # Overlays used by template are added first, as separate step (they can be cloned).
            template_steps = [save_config]
            if self.configuration.data.template.overlays:
                template_steps.append(ProjectInstallationStepAddOverlays(multistage_process=self))
            template_steps.append(ProjectInstallationStepApplyTemplate(multistage_process=self))
            self.stages[1:1] = template_steps
        else:
            self.stages.append(save_config)


class ProjectInstallationStepSaveConfig(MultiStageProcessStage):
    def __init__(
        self,
        multistage_process: MultiStageProcess,
        toolset: Toolset,
        releng_directory: RelengDirectory,
        architecture: Architecture
    ):
        super().__init__(
            name="Save configuration",
            description="Stores metadata about selected components",
            multistage_process=multistage_process
        )
        self.toolset = toolset
        self.releng_directory = releng_directory
        self.architecture = architecture
    def start(self):
        super().start()
        try:
            self.multistage_process.directory.metadata = ProjectConfiguration(
                toolset_id=self.toolset.uuid,
                releng_directory_id=self.releng_directory.id,
                snapshot_id=self.multistage_process.snapshot.filename if self.multistage_process.snapshot else None,
                latest_snapshot=self.multistage_process.latest_snapshot, # Builds get latest snapshot.
                architecture=self.architecture
            )
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during '{self.name}': {e}")
            self.complete(MultiStageProcessStageState.FAILED)

class ProjectInstallationStepAddOverlays(MultiStageProcessStage):
    """Adds overlays used by template for selected options: overlays already added from the same URLs are used, others
    are cloned to overlays."""
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(
            name="Add overlays",
            description="Adds overlays used by template",
            multistage_process=multistage_process
        )
    def start(self):
        super().start()
        try:
            process = self.multistage_process
            template = process.configuration.data.template
            names = template.resolve(process.configuration.data.selected, project_name=process.directory.name)
            overlays = template.generate(names).overlays
            if not overlays:
                self.log("No overlays are used for selected options")
            process.template_overlay_ids = ensure_template_overlays(overlays, log=self.log, progress=self._update_progress)
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            self.log(f"Error: {e}")
            print(f"Error during '{self.name}': {e}")
            self.complete(MultiStageProcessStageState.FAILED)

class ProjectInstallationStepApplyTemplate(MultiStageProcessStage):
    def __init__(self, multistage_process: MultiStageProcess):
        super().__init__(
            name="Apply template",
            description="Creates stages and files of selected template",
            multistage_process=multistage_process
        )
    def start(self):
        super().start()
        try:
            directory = self.multistage_process.directory
            selection = self.multistage_process.configuration.data
            template = selection.template
            temporary_directory = None
            # Commit of template repository (cloned as project), recorded for updates of template.
            template_commit = repository_commit(directory.directory_path()) if selection.repository_url else None
            template_directory_tree = template_tree(directory.directory_path(), selection.template.repository_path) if selection.repository_url else None
            if selection.repository_url:
                # Options were read from template.toml downloaded alone, files come from cloned repository.
                template, temporary_directory = load_cloned_template(directory.directory_path(), selection.repository_url,
                                                                     template.repository_path)
            try:
                names = template.resolve(selection.selected, project_name=directory.name)
                self.log(f"Template: {template.name}")
                for variable in template.visible_variables(names):
                    self.log(f"{variable.title}: {names[variable.id]}")
                # Cloned template repository is replaced with generated content, its history is kept.
                stage_ids = apply_project_template(project_directory=directory, template=template, names=names, log=self.log,
                                                   replace_content=selection.repository_url is not None,
                                                   overlay_ids=getattr(self.multistage_process, "template_overlay_ids", None))
                # Template and generated files are remembered, so project can be updated when template changes.
                record_template_state(directory.directory_path(), template, dict(selection.selected), template_commit, stage_ids,
                                      template_directory_tree)
            finally:
                if temporary_directory:
                    shutil.rmtree(temporary_directory, ignore_errors=True)
            if selection.repository_url:
                path = directory.directory_path()
                # Remote is template repository: project is updated from template (not by pulling template commits),
                # and its changes aren't pushed to template repository.
                self._run(["git", "-C", path, "remote", "remove", "origin"])
                self._run(["git", "-C", path, "add", "--all"])
                if subprocess.run(["git", "-C", path, "diff", "--cached", "--quiet"]).returncode != 0:
                    self._run(["git", "-C", path, "commit", "--quiet", "-m", f"Create project from template {template.name}"])
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            self.log(f"Error: {e}")
            print(f"Error during '{self.name}': {e}")
            self.complete(MultiStageProcessStageState.FAILED)

    def _run(self, command: list[str]):
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in result.stdout.splitlines():
            self.log(line)
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(command)} failed")
