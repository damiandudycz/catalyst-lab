import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.item_select_state import no_valid_items_visible


class EmptySelectionStateTests(unittest.TestCase):
    def test_hides_empty_state_when_none_is_a_single_selection_option(self):
        self.assertFalse(no_valid_items_visible([], display_none=True))

    def test_shows_empty_state_when_multiselect_has_no_items(self):
        self.assertTrue(no_valid_items_visible([], display_none=True, allow_multiselect=True))

    def test_shows_empty_state_when_required_selection_has_no_items(self):
        self.assertTrue(no_valid_items_visible([], display_none=False))

    def test_hides_empty_state_when_valid_items_exist(self):
        self.assertFalse(no_valid_items_visible([object()], display_none=False))

    def test_hides_empty_state_when_current_supported_item_is_locked(self):
        self.assertFalse(no_valid_items_visible([], display_none=False, has_supported_selection=True))

    def test_shows_empty_state_for_unsupported_current_item_without_valid_choices(self):
        self.assertTrue(no_valid_items_visible([], display_none=False, has_supported_selection=False))


if __name__ == '__main__':
    unittest.main()
