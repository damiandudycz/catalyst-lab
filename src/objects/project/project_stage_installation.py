from typing import final
from .project_directory import ProjectDirectory
from .project_stage import ProjectStage
from .multistage_process import (
    MultiStageProcess, MultiStageProcessStage,
    MultiStageProcessStageState
)
import uuid

@final
class ProjectStageInstallation(MultiStageProcess):
    def __init__(
        self,
        project_directory: ProjectDirectory,
        target_name: str,
        releng_template_name: str | None,
        stage_name: str,
        parent_id: uuid.UUID | None,
        toolset=None
    ):
        self.project_directory = project_directory
        self.target_name = target_name
        self.releng_template_name = releng_template_name
        self.stage_name = stage_name
        self.parent_id = parent_id
        self.toolset = toolset
        super().__init__(title="Project stage installation")

    def name(self) -> str:
        return self.target_name

    def setup_stages(self):
        self.stages.append(
            ProjectStageInstallationStepCreate(
                project_directory=self.project_directory,
                target_name=self.target_name,
                releng_template_name=self.releng_template_name,
                stage_name=self.stage_name,
                parent_id=self.parent_id,
                toolset=self.toolset,
                multistage_process=self
            )
        )

    def complete_process(self, success: bool):
        if success:
            self.project_directory.add_stage(self.stage)
            self.project_directory.update_status()

# ------------------------------------------------------------------------------
# Installation process steps.
# ------------------------------------------------------------------------------

class ProjectStageInstallationStepCreate(MultiStageProcessStage):
    def __init__(
        self,
        project_directory: ProjectDirectory,
        target_name: str,
        releng_template_name: str | None,
        stage_name: str,
        parent_id: uuid.UUID | None,
        toolset,
        multistage_process: MultiStageProcess
    ):
        super().__init__(
            name="Create project stage",
            description="Saves stage configuration in project directory",
            multistage_process=multistage_process
        )
        self.project_directory = project_directory
        self.target_name = target_name
        self.releng_template_name = releng_template_name
        self.stage_name = stage_name
        self.parent_id = parent_id
        self.toolset = toolset
    def start(self):
        super().start()
        try:
            stage = ProjectStage(id=None, parent_id=self.parent_id, name=self.stage_name, target_name=self.target_name,
                                 releng_template_name=self.releng_template_name, profile=None)
            self.project_directory.install_stage(
                stage=stage,
                toolset_id=str(self.toolset.uuid) if getattr(self, "toolset", None) else None,
            )
            from .project_stage_baselines import record_stage_creation_baseline
            record_stage_creation_baseline(
                self.project_directory.stage_directory_path(stage.name), stage.serialize()
            )
            self.multistage_process.stage = stage
            self.complete(MultiStageProcessStageState.COMPLETED)
        except Exception as e:
            print(f"Error during '{self.name}': {e}")
            import traceback
            traceback.print_exc()  # This prints the full stack trace to stderr
            self.complete(MultiStageProcessStageState.FAILED)

