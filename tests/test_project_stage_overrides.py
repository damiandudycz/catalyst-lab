"""Library-local metadata for stage defaults and explicit edits."""
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

from catalystlab import project_stage_overrides


class ProjectStageOverridesTests(unittest.TestCase):
    def test_defaults_and_explicit_edits_are_stored_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(project_stage_overrides, "_projects_location", return_value=temporary):
                project_stage_overrides.set_defaults("/projects/demo", "stage-id", {"packages": {"value": "default"}})
                project_stage_overrides.mark_explicit("/projects/demo", "stage-id", "packages")

                metadata = project_stage_overrides.get("/projects/demo", "stage-id")

                self.assertEqual(metadata["defaults"]["packages"], {"value": "default"})
                self.assertEqual(metadata["explicit"], ["packages"])

                project_stage_overrides.mark_explicit("/projects/demo", "stage-id", "packages", explicit=False)
                self.assertEqual(project_stage_overrides.get("/projects/demo", "stage-id")["explicit"], [])

    def test_discard_restores_explicit_markers_from_last_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(project_stage_overrides, "_projects_location", return_value=temporary):
                project_stage_overrides.set_defaults("/projects/demo", "stage-id", {"packages": {"value": "default"}})
                project_stage_overrides.mark_explicit("/projects/demo", "stage-id", "packages")
                project_stage_overrides.checkpoint_committed("/projects/demo")

                project_stage_overrides.mark_explicit("/projects/demo", "stage-id", "releng_template")
                project_stage_overrides.restore_committed("/projects/demo")

                metadata = project_stage_overrides.get("/projects/demo", "stage-id")
                self.assertEqual(metadata["explicit"], ["packages"])
                self.assertEqual(metadata["committed_explicit"], ["packages"])

    def test_discard_clears_uncheckpointed_explicit_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(project_stage_overrides, "_projects_location", return_value=temporary):
                project_stage_overrides.set_defaults("/projects/demo", "stage-id", {"packages": {"value": "default"}})
                project_stage_overrides.mark_explicit("/projects/demo", "stage-id", "packages")

                project_stage_overrides.restore_committed("/projects/demo")

                metadata = project_stage_overrides.get("/projects/demo", "stage-id")
                self.assertEqual(metadata["explicit"], [])
                self.assertEqual(metadata["defaults"], {"packages": {"value": "default"}})


if __name__ == "__main__":
    unittest.main()
