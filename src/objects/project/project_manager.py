from typing import final, Any
import os, shutil, json
from .git_manager import GitManager
from .repository import Repository
from .project_directory import ProjectDirectory
from .project_stage import ProjectStage
from .git_directory import GitDirectoryEvent
from .project_stage import StageArgumentDetails

@final
class ProjectManager(GitManager):

    @classmethod
    def repository(cls) -> Repository:
        return Repository.ProjectDirectory

    def is_stage_name_available(self, project: ProjectDirectory, name: str) -> bool:
        if not name:
            return False
        stage_path = project.stage_directory_path(name=name)
        return not os.path.exists(stage_path)

    def rename_stage(self, project: ProjectDirectory, stage: ProjectStage, name: str):
        if not self.is_stage_name_available(project=project, name=name):
            raise RuntimeError(f"Stage name {name} is not available")
        stage_path_old = project.stage_directory_path(name=stage.name)
        stage_path_new = project.stage_directory_path(name=name)
        shutil.move(stage_path_old, stage_path_new)
        self.change_stage_argument(project=project, stage=stage, argument=StageArgumentDetails.name, value=name)

    def change_stage_argument(self, project: ProjectDirectory, stage: ProjectStage, argument: StageArgumentDetails | str, value: Any):
        """Argument can be passed as plain name for arguments not described by StageArgumentDetails."""
        attribute_name = argument.name if isinstance(argument, StageArgumentDetails) else argument
        setattr(stage, attribute_name, value)
        explicit_none_arguments = getattr(stage, "_explicit_none_arguments", None)
        if explicit_none_arguments is not None:
            if value is None:
                explicit_none_arguments.add(attribute_name)
            else:
                explicit_none_arguments.discard(attribute_name)
        stage._explicit_stage_arguments.add(attribute_name)
        from .project_stage_overrides import mark_explicit
        mark_explicit(project.directory_path(), stage.id, attribute_name)
        self.save_stage(project=project, stage=stage)

    def remove_stage_argument(self, project: ProjectDirectory, stage: ProjectStage, argument: StageArgumentDetails | str):
        """Remove a saved stage setting rather than leaving an explicit None value."""
        attribute_name = argument.name if isinstance(argument, StageArgumentDetails) else argument
        getattr(stage, "_explicit_none_arguments", set()).discard(attribute_name)
        getattr(stage, "_explicit_stage_arguments", set()).discard(attribute_name)
        from .project_stage_overrides import mark_explicit
        mark_explicit(project.directory_path(), stage.id, attribute_name, explicit=False)
        if hasattr(stage, attribute_name):
            delattr(stage, attribute_name)
            self.save_stage(project=project, stage=stage)

    def save_stage(self, project: ProjectDirectory, stage: ProjectStage):
        stage_path = project.stage_directory_path(name=stage.name)
        config_path = os.path.join(stage_path, "stage.json")
        config_json = stage.serialize()
        os.makedirs(stage_path, exist_ok=True)
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config_json, f, indent=4)
        project.event_bus.emit(GitDirectoryEvent.CONTENT_CHANGED, project)

    def stages_using_parent(self, project: ProjectDirectory, stage: ProjectStage) -> list[ProjectStage]:
        """Stages that use given stage as parent (seed)."""
        return [item for item in project.stages if item is not stage and getattr(item, StageArgumentDetails.parent.name, None) == stage.id]

    def remove_stage(self, project: ProjectDirectory, stage: ProjectStage) -> list[ProjectStage]:
        """Deletes stage from project. Stages using it as parent are kept, with parent cleared.
        Returns these detached stages."""
        detached_stages = self.stages_using_parent(project=project, stage=stage)
        for child in detached_stages:
            self.change_stage_argument(project=project, stage=child, argument=StageArgumentDetails.parent, value=None)
        stage_path = project.stage_directory_path(name=stage.name)
        if os.path.isdir(stage_path):
            shutil.rmtree(stage_path)
        if stage in project.stages:
            project.stages.remove(stage)
        project.event_bus.emit(GitDirectoryEvent.CONTENT_CHANGED, project)
        return detached_stages
