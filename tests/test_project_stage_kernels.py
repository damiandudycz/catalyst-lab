"""Kernel settings, inheritance, validation, and Catalyst spec generation."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import project_stage_kernels as kernels
from catalystlab.project_stage_automatic_option import StageAutomaticOption
from catalystlab.project_stage_arguments import StageArgumentDetails
from catalystlab.project_stage_compression_mode import StageCompressionMode
from catalystlab.project_stage_portage_confdir import StagePortageConfdirSource


class KernelSettingsTests(unittest.TestCase):
    def test_kernel_names_read_own_and_releng_values_and_trim_entries(self):
        stage = types.SimpleNamespace(boot_kernel=[' linux ', '', 'rescue '])
        self.assertEqual(kernels.own_kernel_names(stage), ['linux', 'rescue'])
        self.assertEqual(kernels.stage_kernel_names(None, stage), ['linux', 'rescue'])
        self.assertEqual(kernels.own_kernel_names(types.SimpleNamespace(boot_kernel=' linux ')), ['linux'])

    def test_stage_kernel_names_resolve_parent_or_releng_values(self):
        project, stage = object(), object()
        resolver = Mock(return_value=[' vmlinuz ', '', 'Image'])
        with patch.dict(sys.modules, {'catalystlab.project_stage_value_resolver': types.SimpleNamespace(resolve_stage_argument=resolver)}):
            self.assertEqual(kernels.stage_kernel_names(project, stage), ['vmlinuz', 'Image'])
        resolver.assert_called_once_with(project, stage, StageArgumentDetails.boot_kernel.value)

    def test_kernel_settings_prefer_own_then_fall_back_to_releng(self):
        stage = types.SimpleNamespace(boot_kernels={'linux': {'sources': 'own/package'}})
        project = object()
        self.assertEqual(kernels.own_kernel_settings(stage), {'linux': {'sources': 'own/package'}})
        self.assertEqual(kernels.kernel_setting(project, stage, 'linux', 'sources'), ('own/package', True))
        with patch.object(kernels, 'releng_kernel_setting', return_value=['releng/package']):
            self.assertEqual(kernels.kernel_setting(project, stage, 'linux', 'sources'), ('own/package', True))
            self.assertEqual(kernels.kernel_setting(project, stage, 'other', 'sources'), (['releng/package'], False))
        self.assertEqual(kernels.own_kernel_settings(types.SimpleNamespace(boot_kernels=[])), {})

    def test_releng_list_settings_are_normalized_to_lists(self):
        project, stage = object(), object()
        resolver = Mock(return_value={'boot/kernel/linux/packages': 'module-one module-two'})
        with patch.dict(sys.modules, {'catalystlab.project_stage_value_resolver': types.SimpleNamespace(load_stage_releng_template_values=resolver)}):
            self.assertEqual(kernels.releng_kernel_setting(project, stage, 'linux', 'packages'), ['module-one module-two'])
            self.assertIsNone(kernels.releng_kernel_setting(project, stage, 'linux', 'sources'))

    def test_boolean_kernel_setting_accepts_only_yes(self):
        for value in ('yes', ' YES '):
            self.assertTrue(kernels.kernel_setting_enabled(value))
        for value in ('true', '1', 'no', True, None):
            self.assertFalse(kernels.kernel_setting_enabled(value))

    def test_names_validate_uniqueness_and_generate_unused_default(self):
        self.assertIsNone(kernels.kernel_name_error('kernel-2.0_custom', []))
        self.assertIn('already added', kernels.kernel_name_error('kernel', ['kernel']))
        self.assertIn('letters', kernels.kernel_name_error('-bad', []))
        self.assertEqual(kernels.new_kernel_name(['kernel', 'kernel-2']), 'kernel-3')

    def test_inheritance_checks_enum_in_scalar_or_additions_list(self):
        option = StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE
        self.assertTrue(kernels.stage_inherits_kernels(types.SimpleNamespace(boot_kernel=option)))
        self.assertTrue(kernels.stage_inherits_kernels(types.SimpleNamespace(boot_kernel=[option, 'custom'])))
        self.assertFalse(kernels.stage_inherits_kernels(types.SimpleNamespace(boot_kernel=['custom'])))

    def test_template_settings_normalize_flags_lists_numbers_and_empty_values(self):
        result = kernels.kernel_settings_from_template({
            'linux': {
                'sources': 'sys-kernel/gentoo-sources', 'distkernel': True,
                'packages': 'module-one module-two', 'aliases': ['', 'fallback'],
                'extraversion': 2, 'use': [],
            },
            'empty': {'sources': ''},
        }, 'template')
        self.assertEqual(result, {'linux': {
            'sources': 'sys-kernel/gentoo-sources', 'distkernel': 'yes',
            'packages': ['module-one', 'module-two'], 'aliases': ['fallback'], 'extraversion': '2',
        }})

    def test_template_kernel_settings_reject_bad_table_keys_and_types(self):
        class TemplateError(Exception):
            pass
        module = types.SimpleNamespace(TemplateError=TemplateError)
        bad_values = [
            ('not a table', 'kernels must be table'),
            ({'linux': 'bad'}, 'settings of kernel linux must be table'),
            ({'linux': {'unknown': 'x'}}, 'unknown setting unknown'),
            ({'linux': {'packages': [1]}}, 'must be list of texts'),
            ({'linux': {'aliases': 3}}, 'must be list of texts'),
            ({'linux': {'sources': []}}, 'must be text'),
        ]
        for value, message in bad_values:
            with self.subTest(value=value), patch.dict(sys.modules, {'catalystlab.project_template': module}):
                with self.assertRaisesRegex(TemplateError, message):
                    kernels.kernel_settings_from_template(value, 'test template')

    def test_group_kernel_settings_override_matching_values_only(self):
        existing = {'linux': {'sources': 'old', 'use': ['old']}}
        added = {'linux': {'sources': 'new'}, 'rescue': {'sources': 'other'}}
        self.assertEqual(kernels.merge_kernel_settings(existing, added), {
            'linux': {'sources': 'new', 'use': ['old']}, 'rescue': {'sources': 'other'},
        })
        self.assertEqual(existing['linux']['sources'], 'old')

    def test_kernel_spec_values_omit_empty_settings_and_normalize_booleans(self):
        stage = types.SimpleNamespace(boot_kernel=['linux'])
        values = {'sources': 'sys-kernel/gentoo-kernel', 'distkernel': 'no', 'console': ['ttyS0', 'hvc0']}
        def setting(_project, _stage, _name, key):
            return values.get(key), True
        package_module = types.SimpleNamespace(load_kernel_packages=lambda *_: [], derived_distkernel=lambda *_: True)
        with patch.object(kernels, 'kernel_setting', side_effect=setting), \
             patch.dict(sys.modules, {'catalystlab.project_kernel_packages': package_module}):
            spec = kernels.kernel_spec_values(object(), stage)
        self.assertEqual(spec, [
            ('boot/kernel/linux/sources', 'sys-kernel/gentoo-kernel'),
            ('boot/kernel/linux/distkernel', 'yes'),
            ('boot/kernel/linux/console', ['ttyS0', 'hvc0']),
        ])

    def test_iso9660_warning_requires_builtin_filesystem(self):
        self.assertIsNone(kernels.kernel_config_iso9660_warning('CONFIG_ISO9660_FS=y\n'))
        self.assertIn('CONFIG_ISO9660_FS=y', kernels.kernel_config_iso9660_warning('CONFIG_ISO9660_FS=m\n'))

    def test_live_cd_warning_applies_only_to_genkernel_kernel_and_reads_config(self):
        stage = types.SimpleNamespace(target='livecd-stage2', name='stage2')
        project = types.SimpleNamespace(stage_directory_path=lambda **_: '/stage', directory_path=lambda: '/project',
                                        get_releng_directory=lambda: None)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory, 'kernel.config')
            config.write_text('CONFIG_ISO9660_FS=m\n')
            with patch.object(kernels, 'kernel_setting', side_effect=[('no', True), (str(config), True)]):
                self.assertIn('Media not found', kernels.live_cd_kernel_warning(project, stage, 'linux'))
        with patch.object(kernels, 'kernel_setting', return_value=('yes', True)):
            self.assertIsNone(kernels.live_cd_kernel_warning(project, stage, 'linux'))
        self.assertIsNone(kernels.live_cd_kernel_warning(project, types.SimpleNamespace(target='stage4'), 'linux'))


if __name__ == '__main__':
    unittest.main()
