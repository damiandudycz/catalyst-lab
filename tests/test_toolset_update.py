"""Portage progress parsing used by toolset updates."""
from pathlib import Path
import sys
import types
import unittest
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from gi.repository import Gio
from catalystlab.toolset_application import (
    PortageConfig, ToolsetApplication, ToolsetApplicationInstall,
    ToolsetApplicationSelection, ToolsetApplicationVersion,
)
from catalystlab.toolset_installation import app_portage_files
from catalystlab.toolset_update import ToolsetUpdate, _emerge_progress, _webrsync_progress


class ToolsetUpdateTests(unittest.TestCase):
    def test_emerge_progress_parses_completed_package_count(self):
        self.assertEqual(_emerge_progress('>>> Completed (3 of 12)'), 0.25)

    def test_emerge_progress_ignores_other_or_empty_counts(self):
        self.assertIsNone(_emerge_progress('>>> Emerging (3 of 12) package'))
        self.assertIsNone(_emerge_progress('>>> Completed (0 of 0)'))

    def test_webrsync_progress_reads_percentage_and_ignores_other_output(self):
        self.assertEqual(_webrsync_progress('45500K .... 50% 3M =2.2s'), 0.5)
        self.assertIsNone(_webrsync_progress('downloading metadata'))

    def test_app_portage_files_collects_configs_and_new_patch_files(self):
        with tempfile.TemporaryDirectory() as directory:
            patch_path = Path(directory) / 'change.patch'
            patch_path.write_text('patch contents\n', encoding='utf-8')
            app = types.SimpleNamespace(name='Example', package='category/package')
            version = ToolsetApplicationVersion(name='Stable', id=None, config=(
                PortageConfig(directory='package.use', entries=('category/package feature',)),
            ))
            selection = ToolsetApplicationSelection(app, version, True, [
                Gio.File.new_for_path(str(patch_path)), 'already-installed.patch',
            ])

            self.assertEqual(app_portage_files(selection), {
                'package.use/Example': 'category/package feature\n',
                'patches/category/package/change.patch': 'patch contents\n',
            })

    def test_update_selection_keeps_new_apps_skips_unchanged_and_removes_omitted(self):
        new_app = types.SimpleNamespace(package='category/new', dependencies=[])
        unchanged_app = types.SimpleNamespace(package='category/unchanged', dependencies=[])
        removed_app = types.SimpleNamespace(package='category/removed', dependencies=[])
        version = types.SimpleNamespace(id='version-1')
        unchanged_install = ToolsetApplicationInstall(version='1', variant=version, patches=[])
        update = object.__new__(ToolsetUpdate)
        update.apps_selection = [
            ToolsetApplicationSelection(new_app, version, True, []),
            ToolsetApplicationSelection(unchanged_app, version, True, []),
        ]
        installs = {
            id(unchanged_app): unchanged_install,
            id(removed_app): ToolsetApplicationInstall(version='1', variant=version, patches=[]),
        }
        update.toolset = types.SimpleNamespace(get_app_install=lambda app: installs.get(id(app)))

        # ToolsetApplication.ALL holds concrete application instances. The classifier only needs this list.
        with patch.object(ToolsetApplication, 'ALL', [new_app, unchanged_app, removed_app]):
            update._process_selected_apps()

        self.assertEqual([selection.app for selection in update.apps_selection], [new_app])
        self.assertEqual(update.apps_to_remove, [removed_app])


if __name__ == '__main__':
    unittest.main()
