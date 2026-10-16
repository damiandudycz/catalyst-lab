"""Automatic project-stage values, inheritance, and releng spec parsing."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.architecture import Architecture
from catalystlab.project_stage_automatic_option import StageAutomaticOption as Automatic
from catalystlab.project_stage_arguments import StageArgumentDetails as Argument
from catalystlab.project_stage_value_resolver import (
    UNRESOLVED, _convert_releng_value, _parse_spec_file, format_stage_argument_value,
    load_stage_releng_template_values, resolve_stage_argument, split_additions,
)


class StageValueResolutionTests(unittest.TestCase):
    def test_direct_parent_and_missing_parent_values(self):
        parent = types.SimpleNamespace(id=uuid.uuid4(), version_stamp='20261016', target='stage3')
        child = types.SimpleNamespace(id=uuid.uuid4(), parent=parent.id,
                                      version_stamp=Automatic.INHERIT_FROM_PARENT)
        project = types.SimpleNamespace(stages=[parent, child])
        self.assertEqual(resolve_stage_argument(project, child, 'version_stamp'), '20261016')
        child.parent = uuid.uuid4()
        self.assertIs(resolve_stage_argument(project, child, 'version_stamp'), UNRESOLVED)
        child.version_stamp = 'custom'
        self.assertEqual(resolve_stage_argument(project, child, 'version_stamp'), 'custom')
        self.assertIsNone(resolve_stage_argument(project, types.SimpleNamespace(id=uuid.uuid4()), 'version_stamp'))

    def test_parent_cycles_are_unresolved_instead_of_recursing_forever(self):
        first_id, second_id = uuid.uuid4(), uuid.uuid4()
        first = types.SimpleNamespace(id=first_id, parent=second_id, version_stamp=Automatic.INHERIT_FROM_PARENT)
        second = types.SimpleNamespace(id=second_id, parent=first_id, version_stamp=Automatic.INHERIT_FROM_PARENT)
        project = types.SimpleNamespace(stages=[first, second])
        self.assertIs(resolve_stage_argument(project, first, 'version_stamp'), UNRESOLVED)

    def test_additions_merge_inherited_lists_without_duplicates(self):
        parent = types.SimpleNamespace(id=uuid.uuid4(), stage4_packages=['app-editors/vim', 'app-shells/bash'])
        child = types.SimpleNamespace(id=uuid.uuid4(), parent=parent.id,
            stage4_packages=[Automatic.INHERIT_FROM_PARENT, 'app-shells/bash', 'sys-apps/less'])
        project = types.SimpleNamespace(stages=[parent, child])
        self.assertEqual(resolve_stage_argument(project, child, Argument.stage4_packages.value),
                         ['app-editors/vim', 'app-shells/bash', 'sys-apps/less'])

    def test_additions_join_text_values_in_order_and_releng_can_be_added(self):
        child = types.SimpleNamespace(id=uuid.uuid4(), cflags=[Automatic.INHERIT_FROM_RELENG_TEMPLATE, '-O3'])
        project = types.SimpleNamespace(stages=[child])
        with patch('catalystlab.project_stage_value_resolver.load_stage_releng_template_values',
                   return_value={'cflags': '-pipe -march=native'}):
            self.assertEqual(resolve_stage_argument(project, child, 'cflags'), '-pipe -march=native -O3')
        self.assertEqual(split_additions([Automatic.INHERIT_FROM_PARENT, 'one', Automatic.INHERIT_FROM_PARENT]),
                         ([Automatic.INHERIT_FROM_PARENT], ['one']))
        self.assertIsNone(split_additions([Automatic.INHERIT_FROM_PARENT]))
        self.assertIsNone(split_additions(['plain', 'values']))

    def test_generated_source_path_uses_parent_rel_type_subarch_and_version(self):
        parent = types.SimpleNamespace(id=uuid.uuid4(), target='stage3_openrc', rel_type='default',
                                       subarch='arm64', version_stamp='20261016')
        child = types.SimpleNamespace(id=uuid.uuid4(), parent=parent.id,
                                      source_subpath=Automatic.GENERATE_AUTOMATICALLY)
        project = types.SimpleNamespace(stages=[parent, child])
        self.assertEqual(resolve_stage_argument(project, child, 'source_subpath'),
                         'default/stage3-openrc-arm64-20261016')

    def test_releng_spec_parser_handles_comments_continuations_empty_values_and_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stage.spec'
            path.write_text('target: stage3 # comment\nsubarch: arm64\n    armv7a\nempty:\nprofile: default/linux/arm64/23.0\n')
            parsed = _parse_spec_file(str(path), path.stat().st_mtime)
            self.assertEqual(parsed, {'target': 'stage3', 'subarch': ['arm64', 'armv7a'],
                                      'profile': 'default/linux/arm64/23.0'})
            self.assertIs(_parse_spec_file(str(path), path.stat().st_mtime), parsed)

    def test_releng_values_require_template_releases_architecture_and_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            releng_root = Path(directory) / 'releng'
            spec = releng_root / 'releases' / 'specs' / 'arm64' / 'stage3'
            spec.parent.mkdir(parents=True)
            spec.write_text('version_stamp: 20261016\n')
            releng = types.SimpleNamespace(directory_path=lambda: str(releng_root))
            project = types.SimpleNamespace(get_releng_directory=lambda: releng,
                                            get_architecture=lambda: Architecture.arm64)
            stage = types.SimpleNamespace(releng_template='stage3')
            self.assertEqual(load_stage_releng_template_values(project, stage), {'version_stamp': '20261016'})
            self.assertIsNone(load_stage_releng_template_values(project, types.SimpleNamespace(releng_template=None)))
            project.get_architecture = lambda: None
            self.assertIsNone(load_stage_releng_template_values(project, stage))

    def test_releng_values_convert_to_stage_argument_types(self):
        self.assertEqual(_convert_releng_value(Argument.stage4_packages, 'one'), ['one'])
        self.assertEqual(_convert_releng_value(Argument.update_seed, 'YES'), True)
        self.assertEqual(_convert_releng_value(Argument.update_seed, 'no'), False)
        self.assertEqual(_convert_releng_value(Argument.cflags, ['-O2', '-pipe']), '-O2 -pipe')
        self.assertIsNone(_convert_releng_value(Argument.cflags, None))

    def test_formatted_values_handle_missing_booleans_and_limited_lists(self):
        self.assertIsNone(format_stage_argument_value(UNRESOLVED))
        self.assertEqual(format_stage_argument_value(True), 'Yes')
        self.assertEqual(format_stage_argument_value(False), 'No')
        self.assertEqual(format_stage_argument_value(['one', 'two', 'three', 'four']), 'one, two, three (+1 more)')
        self.assertIsNone(format_stage_argument_value([]))
        self.assertEqual(format_stage_argument_value('value'), 'value')


if __name__ == '__main__':
    unittest.main()
