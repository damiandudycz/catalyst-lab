"""Default toolset preference and per-action compatibility selection."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import toolset_selection as selection
from catalystlab.repository import Repository
from catalystlab.toolset_application import ToolsetApplication


class ToolsetSelectionTests(unittest.TestCase):
    def setUp(self):
        self.catalyst = ToolsetApplication.CATALYST
        self.qemu = ToolsetApplication.QEMU
        self.old = self._toolset("old", 10, (self.catalyst,))
        self.preferred = self._toolset("preferred", 5, (self.catalyst, self.qemu))
        self.newest = self._toolset("newest", 20, (self.catalyst, self.qemu))
        settings = types.SimpleNamespace(default_toolset_id=str(self.preferred.uuid))
        self.settings_repository = types.SimpleNamespace(value=settings)

    @staticmethod
    def _toolset(name, updated, apps):
        item = Mock()
        item.name = name
        item.uuid = uuid.uuid4()
        item.metadata = {"date_updated": updated}
        item.busy_reason = None
        item.get_app_install.side_effect = lambda app: object() if app in apps else None
        return item

    def test_configured_toolset_is_preferred_when_compatible(self):
        with patch.object(Repository, "Settings", self.settings_repository, create=True):
            result = selection.default_toolset((self.catalyst,), [self.old, self.preferred, self.newest])
            ordered = selection.sorted_toolsets([self.old, self.preferred, self.newest], (self.catalyst,))
        self.assertIs(result, self.preferred)
        self.assertIs(ordered[0], self.preferred)

    def test_uses_newest_compatible_when_preferred_lacks_required_application(self):
        self.preferred.get_app_install.side_effect = lambda app: object() if app == self.catalyst else None
        with patch.object(Repository, "Settings", self.settings_repository, create=True):
            result = selection.default_toolset((self.qemu,), [self.old, self.preferred, self.newest])
        self.assertIs(result, self.newest)

    def test_uses_newest_compatible_when_preferred_is_busy(self):
        self.preferred.busy_reason = "reserved"
        with patch.object(Repository, "Settings", self.settings_repository, create=True):
            result = selection.default_toolset((self.catalyst,), [self.old, self.preferred, self.newest])
        self.assertIs(result, self.newest)


if __name__ == "__main__":
    unittest.main()
