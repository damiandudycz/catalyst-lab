from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.project_stage import ProjectStage, apply_default_stage_arguments
from catalystlab.project_stage_arguments import StageArgumentDetails, StageArgumentTargetDetails
from catalystlab.project_stage_automatic_option import StageAutomaticOption
from catalystlab.project_stage_value_resolver import UNRESOLVED


class StageDefaultTests(unittest.TestCase):
    def test_automatic_and_empty_values_stay_unset_but_concrete_template_values_are_selected(self):
        stage = ProjectStage(name='stage1', target_name='stage1', releng_template_name='template.spec')
        project = types.SimpleNamespace(stages=[])
        arguments = {
            'interpreter': StageArgumentTargetDetails(
                'interpreter', False, StageArgumentDetails.interpreter
            ),
            'compression_mode': StageArgumentTargetDetails(
                'compression_mode', False, StageArgumentDetails.compression_mode
            ),
            'profile': StageArgumentTargetDetails(
                'profile', False, StageArgumentDetails.profile
            ),
        }

        def resolve(_project, _stage, _name, option=None):
            if option == StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE:
                return 'default/linux/arm64/23.0' if _name == 'profile' else UNRESOLVED
            return UNRESOLVED

        with patch('catalystlab.project_stage.load_catalyst_stage_arguments_details', return_value=arguments), \
             patch('catalystlab.project_stage_value_resolver.resolve_stage_argument', side_effect=resolve):
            applied = apply_default_stage_arguments(project, stage, toolset=object())

        self.assertEqual(applied, {'profile': StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE})
        self.assertFalse(hasattr(stage, 'interpreter'))
        self.assertFalse(hasattr(stage, 'compression_mode'))
        self.assertEqual(stage.profile, StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE)
        self.assertEqual(stage._default_stage_arguments, applied)


if __name__ == '__main__':
    unittest.main()
