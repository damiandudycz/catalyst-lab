"""Descriptions shared between Catalyst stage targets."""
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.project_stage_argument_descriptions import stage_argument_description


class StageArgumentDescriptionTests(unittest.TestCase):
    def test_livecd_descriptions_apply_to_other_targets_with_adjusted_wording(self):
        self.assertEqual(stage_argument_description('livecd/root_overlay'),
                         'Files copied into filesystem of live system. Sources selected here are combined.')
        self.assertEqual(stage_argument_description('stage3/root_overlay'),
                         'Files copied into filesystem of image.')

    def test_stage4_common_options_apply_to_other_targets(self):
        self.assertEqual(stage_argument_description('stage4/groups'), 'Groups created in stage.')
        self.assertEqual(stage_argument_description('stage3/groups'), 'Groups created in stage.')

    def test_unknown_option_has_no_description(self):
        self.assertIsNone(stage_argument_description('stage3/unknown'))


if __name__ == '__main__':
    unittest.main()
