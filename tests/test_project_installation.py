"""Project installation cleanup for downloaded template files."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if "catalystlab" not in sys.modules:
    package = types.ModuleType("catalystlab")
    package.__path__ = [str(ROOT / "src")] + [str(path) for path in (ROOT / "src").rglob("*") if path.is_dir()]
    sys.modules["catalystlab"] = package

from catalystlab import project_installation as module
from catalystlab import project_stage_installation as stage_module
from catalystlab.multistage_process import MultiStageProcessStage, MultiStageProcessStageState
from catalystlab.project_installation import ProjectInstallationStepApplyTemplate
from catalystlab.project_stage_installation import ProjectStageInstallation, ProjectStageInstallationStepCreate


class ProjectInstallationTests(unittest.TestCase):
    def test_downloaded_app_template_is_removed_when_later_metadata_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = types.SimpleNamespace(name="project", directory_path=lambda: "/tmp/project")
            template = types.SimpleNamespace(
                source_kind="app", files_available=False, repository_path="template", source_tree="tree",
                resolve=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("invalid template")),
            )
            selection = types.SimpleNamespace(template=template, repository_url=None, selected={})
            process = types.SimpleNamespace(
                directory=directory,
                configuration=types.SimpleNamespace(data=selection),
            )
            downloaded = types.SimpleNamespace(**template.__dict__)
            step = object.__new__(ProjectInstallationStepApplyTemplate)
            step.multistage_process = process
            step.name = "Apply template"
            temporary_directory = Path(temporary) / "downloaded-template"
            temporary_directory.mkdir()
            completed = []

            with patch.object(MultiStageProcessStage, "start"), \
                 patch.object(MultiStageProcessStage, "log"), \
                 patch.object(MultiStageProcessStage, "complete", side_effect=lambda state: completed.append(state)), \
                 patch.object(module, "download_app_template", return_value=(downloaded, str(temporary_directory))):
                step.start()

            self.assertFalse(temporary_directory.exists())
            self.assertEqual(completed, [MultiStageProcessStageState.FAILED])

    def create_stage_step(self, project_directory, process):
        step = object.__new__(ProjectStageInstallationStepCreate)
        step.project_directory = project_directory
        step.target_name = "stage4"
        step.releng_template_name = None
        step.stage_name = "Stage 4"
        step.parent_id = None
        step.toolset = types.SimpleNamespace(uuid="toolset-uuid")
        step.multistage_process = process
        step.name = "Create project stage"
        return step

    def test_stage_creation_installs_stage_and_completes(self):
        project = types.SimpleNamespace(install_stage=unittest.mock.Mock())
        process = types.SimpleNamespace()
        step = self.create_stage_step(project, process)
        stage = object()
        completed = []

        with patch.object(stage_module, "ProjectStage", return_value=stage), \
             patch.object(stage_module, "apply_default_stage_arguments", return_value={}), \
             patch.object(MultiStageProcessStage, "start"), \
             patch.object(MultiStageProcessStage, "log"), \
             patch.object(MultiStageProcessStage, "complete", side_effect=completed.append):
            step.start()

        project.install_stage.assert_called_once_with(stage=stage)
        self.assertIs(process.stage, stage)
        self.assertEqual(completed, [MultiStageProcessStageState.COMPLETED])

    def test_stage_creation_continues_when_default_arguments_fail(self):
        project = types.SimpleNamespace(install_stage=unittest.mock.Mock())
        process = types.SimpleNamespace()
        step = self.create_stage_step(project, process)
        completed = []
        log = unittest.mock.Mock()

        with patch.object(stage_module, "ProjectStage", return_value=object()), \
             patch.object(stage_module, "apply_default_stage_arguments", side_effect=RuntimeError("defaults unavailable")), \
             patch.object(MultiStageProcessStage, "start"), \
             patch.object(MultiStageProcessStage, "log", side_effect=log), \
             patch.object(MultiStageProcessStage, "complete", side_effect=completed.append):
            step.start()

        project.install_stage.assert_called_once()
        self.assertEqual(completed, [MultiStageProcessStageState.COMPLETED])
        self.assertIn("Failed to set default values", log.call_args.args[0])

    def test_stage_creation_reports_installation_failure(self):
        project = types.SimpleNamespace(install_stage=unittest.mock.Mock(side_effect=RuntimeError("disk full")))
        process = types.SimpleNamespace()
        step = self.create_stage_step(project, process)
        completed = []

        with patch.object(stage_module, "ProjectStage", return_value=object()), \
             patch.object(stage_module, "apply_default_stage_arguments", return_value={}), \
             patch.object(MultiStageProcessStage, "start"), \
             patch.object(MultiStageProcessStage, "log"), \
             patch.object(MultiStageProcessStage, "complete", side_effect=completed.append), \
             patch("traceback.print_exc"):
            step.start()

        self.assertEqual(completed, [MultiStageProcessStageState.FAILED])

    def test_installation_only_updates_project_after_success(self):
        project = types.SimpleNamespace(add_stage=unittest.mock.Mock(), update_status=unittest.mock.Mock())
        installation = object.__new__(ProjectStageInstallation)
        installation.project_directory = project
        installation.stage = object()

        installation.complete_process(False)
        project.add_stage.assert_not_called()
        project.update_status.assert_not_called()

        installation.complete_process(True)
        project.add_stage.assert_called_once_with(installation.stage)
        project.update_status.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
