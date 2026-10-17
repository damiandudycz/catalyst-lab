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
from catalystlab.multistage_process import MultiStageProcessStage, MultiStageProcessStageState
from catalystlab.project_installation import ProjectInstallationStepApplyTemplate


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


if __name__ == "__main__":
    unittest.main()
