"""Catalyst stage-spec value formatting."""
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.project_build_spec import _copy_folders, _format_spec_line, select_seed_url


class StageSpecFormattingTests(unittest.TestCase):
    def test_formats_multiline_values_and_replaces_placeholders(self):
        line = _format_spec_line('boot/kernel/linux/packages',
                                 ['@PROJECT_DIR@/kernel', '@TIMESTAMP@'], str,
                                 {'@PROJECT_DIR@': '/project', '@TIMESTAMP@': '20261017'})

        self.assertEqual(line, 'boot/kernel/linux/packages: \n\t/project/kernel\n\t20261017')

    def test_empty_values_are_omitted(self):
        self.assertIsNone(_format_spec_line('stage4/packages', [], str, {}))

    def test_folders_merge_in_order_and_keep_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second, destination = root / 'first', root / 'second', root / 'result'
            first.mkdir(); second.mkdir()
            (first / 'setting').write_text('first')
            (first / 'link').symlink_to('target')
            (second / 'setting').write_text('second')

            _copy_folders([str(first), str(root / 'missing'), str(second)], str(destination))

            self.assertEqual((destination / 'setting').read_text(), 'second')
            self.assertTrue((destination / 'link').is_symlink())

    def test_seed_selection_requires_exact_stage3_timestamped_archive(self):
        urls = [
            types.SimpleNamespace(path="stage3-arm64-openrc-20261017T100000Z.tar.xz"),
            types.SimpleNamespace(path="stage3-arm64-openrc-desktop-20261017T100000Z.tar.xz"),
            types.SimpleNamespace(path="stage3-arm64-openrc-latest.tar.xz"),
        ]

        self.assertIs(urls[0], select_seed_url(urls, "stage3-arm64-openrc"))
        self.assertIsNone(select_seed_url(urls[1:], "stage3-arm64-openrc"))


if __name__ == '__main__':
    unittest.main()
