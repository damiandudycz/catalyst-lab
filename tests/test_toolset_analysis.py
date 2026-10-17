"""Toolset analysis should not save partial metadata after a failed check."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.toolset import Toolset
from catalystlab.toolset_application import ToolsetApplication


class ToolsetAnalysisTests(unittest.TestCase):
    def test_failed_check_returns_no_metadata_and_does_not_save(self):
        toolset = Toolset.__new__(Toolset)
        toolset.metadata = {}
        app = object()
        with (
            patch.object(ToolsetApplication, 'ALL', [app]),
            patch.object(toolset, '_perform_app_installed_version_check', side_effect=RuntimeError('failed')),
            patch.object(toolset, '_perform_app_additional_checks'),
            patch.object(toolset, 'replace_metadata') as save,
        ):
            self.assertIsNone(toolset.analyze(save=True))
        save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
