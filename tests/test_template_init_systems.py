"""Consistent init-system choices and dev tools across official templates."""
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
from catalystlab.project_template import ProjectTemplate


class TemplateInitSystemTests(unittest.TestCase):
    def test_official_templates_share_multi_init_selector(self):
        version = patch.object(app_info, 'APP_VERSION', '2.0.0')
        version.start()
        self.addCleanup(version.stop)

        for template_path in (ROOT / 'data/project_templates').glob('*/template.toml'):
            with self.subTest(template=template_path.parent.name):
                template = ProjectTemplate.load(str(template_path.parent))
                selector = next((variable for variable in template.variables
                                 if variable.id in ('init', 'init_systems')), None)
                if selector is None:
                    continue
                self.assertEqual(selector.id, 'init_systems')
                self.assertEqual(selector.title, 'Init systems')
                self.assertEqual(selector.type, 'multiple')
                self.assertEqual(selector.minimum_selected, 1)
                self.assertEqual(selector.default, ['openrc'])
                self.assertEqual([option.value for option in selector.options], ['openrc', 'systemd'])
                self.assertEqual(template.resolve({'init': 'systemd'})['init_systems'], ['systemd'])

    def test_distcc_is_only_in_playstation_template(self):
        templates = list((ROOT / 'data/project_templates').glob('*/template.toml'))
        containing_distcc = []
        for template_path in templates:
            text = template_path.read_text()
            if 'sys-devel/distcc' in text:
                containing_distcc.append(template_path.parent.name)
            if template_path.parent.name != 'playstation-3':
                self.assertNotIn('distcc', text.lower(), template_path.parent.name)
        self.assertEqual(containing_distcc, ['playstation-3'])

    def test_openrc_only_extras_are_only_generated_for_openrc_variant(self):
        version = patch.object(app_info, 'APP_VERSION', '2.0.0')
        version.start()
        self.addCleanup(version.stop)

        for name, extra_stages in (
            ('gentoo-release', ['iso', 'livegui']),
            ('playstation-3', ['iso', 'xfce']),
            ('asus-rog-ally', ['gnome', 'plasma', 'iso', 'iso_gui']),
        ):
            template = ProjectTemplate.load(str(ROOT / 'data/project_templates' / name))
            names = template.resolve({
                'init_systems': ['openrc', 'systemd'],
                'accept_all_licenses': True,
                'extra_stages': extra_stages,
            })
            # Stage argument serialization needs GTK's GI modules, which aren't required for
            # checking the generated stage graph and conditions.
            with patch('catalystlab.project_template._template_argument_value', side_effect=lambda _key, value, _context: value):
                generated = template.generate(names)
            generated_ids = {stage.template_id for stage in generated.stages}
            if name == 'gentoo-release':
                self.assertIn('livecd-stage1-openrc', generated_ids)
                self.assertNotIn('livecd-stage1-systemd', generated_ids)
                self.assertIn('livegui-stage1-openrc', generated_ids)
                self.assertNotIn('livegui-stage1-systemd', generated_ids)
            elif name == 'playstation-3':
                self.assertIn('livecd-stage1-openrc', generated_ids)
                self.assertNotIn('livecd-stage1-systemd', generated_ids)
                self.assertIn('stage3-desktop-openrc', generated_ids)
                self.assertNotIn('stage3-desktop-systemd', generated_ids)
            else:
                self.assertIn('stage3-desktop-openrc', generated_ids)
                self.assertIn('stage3-desktop-systemd', generated_ids)


if __name__ == '__main__':
    unittest.main()
