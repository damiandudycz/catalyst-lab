from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.project_stage_arguments import (
    StageArgumentDetails,
    StageArgumentOption,
    StageArgumentType,
    parent_stage,
    visible_stage_argument_options,
)


class VisibleStageArgumentOptionsTests(unittest.TestCase):
    def test_hides_unsupported_unselected_values(self):
        supported = StageArgumentOption("stage4", "stage4", None, "stage4", None)
        unsupported = StageArgumentOption("old", "Unsupported value", None, "old", None, unsupported=True)

        self.assertEqual(visible_stage_argument_options([unsupported, supported], []), [supported])

    def test_keeps_selected_unsupported_values_available_for_recovery(self):
        unsupported = StageArgumentOption("old", "Unsupported value", None, "old", None, unsupported=True)

        self.assertEqual(visible_stage_argument_options([unsupported], ["old"]), [unsupported])


class ParentStageTests(unittest.TestCase):
    def test_resolves_parent_and_returns_none_when_missing(self):
        parent = types.SimpleNamespace(id="parent")
        child = types.SimpleNamespace(id="child", parent="parent")
        project = types.SimpleNamespace(stages=[child, parent])

        self.assertIs(parent_stage(project, child), parent)
        self.assertIsNone(parent_stage(project, types.SimpleNamespace(id="orphan", parent="missing")))
        self.assertIsNone(parent_stage(project, types.SimpleNamespace()))


class StageArgumentMetadataTests(unittest.TestCase):
    def test_every_argument_has_a_display_name_and_editor_type(self):
        for argument in StageArgumentDetails:
            with self.subTest(argument=argument):
                self.assertTrue(argument.display_name)
                self.assertIsInstance(argument.type, StageArgumentType)

    def test_target_specific_arguments_keep_distinct_values_and_shared_labels(self):
        self.assertEqual(StageArgumentDetails.stage4_packages.display_name, "Packages")
        self.assertEqual(StageArgumentDetails.livecd_packages.display_name, "Packages")
        self.assertEqual(StageArgumentDetails.stage4_packages.value, "stage4/packages")
        self.assertEqual(StageArgumentDetails.livecd_packages.value, "livecd/packages")


if __name__ == "__main__":
    unittest.main()
