"""Opt-in license handling on device and general Gentoo templates."""
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

from catalystlab import app_info
from catalystlab.project_template import ProjectTemplate, evaluate_condition


class TemplateLicenseHandlingTests(unittest.TestCase):
    def test_non_pi_templates_gate_stage4_and_add_license_configuration(self):
        expected_stage4 = {
            'asus-rog-ally': {'stage4', 'stage4-gnome', 'stage4-gnome-extras', 'stage4-plasma'},
            'gentoo-release': {'stage4'},
            'playstation-3': {'stage4', 'stage4-desktop'},
        }
        version = patch.object(app_info, 'APP_VERSION', '2.0.0')
        version.start()
        self.addCleanup(version.stop)

        for template_name, stage_ids in expected_stage4.items():
            with self.subTest(template=template_name):
                template_path = ROOT / 'data/project_templates' / template_name
                template = ProjectTemplate.load(str(template_path))
                license_option = next(variable for variable in template.variables if variable.id == 'accept_all_licenses')
                self.assertFalse(license_option.default)

                names = {
                    'accept_all_licenses': False,
                    'extra_stages': ['stage4', 'gnome', 'gnome_customized', 'plasma', 'xfce'],
                    'init': 'openrc',
                    'with_desktop': False,
                    'gnome_customizations': True,
                }
                enabled_names = {**names, 'accept_all_licenses': True, 'with_desktop': True}
                for stage in template.stages:
                    if stage.id in stage_ids:
                        self.assertFalse(evaluate_condition(stage.when, names), stage.id)
                        self.assertTrue(evaluate_condition(stage.when, enabled_names), stage.id)

                license_files = [item for item in template.files if item.source == 'files/accept-all-licenses']
                self.assertTrue(license_files)
                active_license_files = [item for item in license_files
                                        if evaluate_condition(item.when, enabled_names)]
                inactive_license_files = [item for item in license_files
                                          if not evaluate_condition(item.when, names)]
                self.assertEqual(len(active_license_files), len(stage_ids))
                self.assertEqual(len(inactive_license_files), len(license_files))
                self.assertTrue((template_path / 'files/accept-all-licenses/package.license/90-accept-all-licenses').is_file())


if __name__ == '__main__':
    unittest.main()
