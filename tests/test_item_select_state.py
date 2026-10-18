import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.item_select_state import ItemSelectionData, no_valid_items_visible
from catalystlab.item_select_view import update_selection_messages


class SelectionData(ItemSelectionData):
    def __init__(self):
        self.reloads = 0

    def _load_items(self):
        self.reloads += 1


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

    def test_shared_message_helper_supports_expander_rows(self):
        class Label:
            def set_visible(self, visible):
                self.visible = visible

        class Expander:
            def __init__(self):
                self.selected_item = None
                self.display_none = False
                self.no_valid_entries_label = Label()
                self.is_not_usable_label = Label()
                self.available = object()
                self.items_list = [self.available, object()]

            def items(self):
                return self.items_list

            def emit(self, signal, item):
                return signal == "is-item-selectable" and item is self.available

        view = Expander()
        update_selection_messages(view)

        self.assertFalse(view.no_valid_entries_label.visible)
        self.assertFalse(view.is_not_usable_label.visible)


class ItemSelectionDataTests(unittest.TestCase):
    def test_items_combine_leading_and_repository_items(self):
        view = SelectionData()
        view.repository = type("Repository", (), {"value": ["repo"]})()
        view.set_leading_items(["leading"])

        self.assertEqual(view.items(), ["leading", "repo"])
        self.assertEqual(view.reloads, 0)

    def test_static_items_reload_and_replace_repository(self):
        view = SelectionData()
        view.repository = type("Repository", (), {"value": ["repo"]})()
        view.set_static_list(items=["static"])

        self.assertEqual(view.items(), ["static"])
        self.assertEqual(view.reloads, 1)

    def test_leading_items_reload_after_rows_exist(self):
        view = SelectionData()
        view.rows = []
        view.set_leading_items(["leading"])

        self.assertEqual(view.reloads, 1)


if __name__ == '__main__':
    unittest.main()
