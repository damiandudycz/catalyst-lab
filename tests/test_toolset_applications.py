"""Toolset application selections include and order their dependencies."""
from pathlib import Path
import sys
import types
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.toolset_application import ToolsetApplicationSelection, order_selected_applications


def selection(app, selected=False):
    return ToolsetApplicationSelection(app, None, selected, ())


class ToolsetApplicationSelectionTests(unittest.TestCase):
    def test_selects_transitive_dependencies_before_dependents(self):
        base = SimpleNamespace(name='base', dependencies=())
        middle = SimpleNamespace(name='middle', dependencies=(base,))
        chosen = SimpleNamespace(name='chosen', dependencies=(middle,))
        unrelated = SimpleNamespace(name='unrelated', dependencies=())

        result = order_selected_applications([
            selection(chosen, True), selection(unrelated), selection(base), selection(middle),
        ])

        self.assertEqual([item.app.name for item in result], ['base', 'middle', 'chosen'])
        self.assertTrue(all(item.selected for item in result))

    def test_missing_dependencies_are_skipped_and_cycles_terminate(self):
        first = SimpleNamespace(name='first', dependencies=())
        second = SimpleNamespace(name='second', dependencies=(first,))
        first.dependencies = (second, SimpleNamespace(name='missing', dependencies=()))

        result = order_selected_applications([selection(first, True), selection(second)])

        self.assertEqual([item.app.name for item in result], ['second', 'first'])
        self.assertTrue(all(item.selected for item in result))

    def test_none_means_no_selection_request(self):
        self.assertIsNone(order_selected_applications(None))


if __name__ == '__main__':
    unittest.main()
