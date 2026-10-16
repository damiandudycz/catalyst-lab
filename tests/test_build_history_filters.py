"""Build-history filters partition terminal outcomes without hiding stages in All."""
import ast
from enum import Enum
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BuildHistoryFilterTests(unittest.TestCase):
    def test_each_state_matches_expected_filters(self):
        path = ROOT / 'src/ui/project/project_builds_view.py'
        tree = ast.parse(path.read_text())
        nodes = [node for node in tree.body if
                 isinstance(node, ast.ClassDef) and node.name == 'BuildRowState' or
                 isinstance(node, ast.FunctionDef) and node.name == '_matches_filter']
        namespace = {'Enum': Enum}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
        state, matches = namespace['BuildRowState'], namespace['_matches_filter']
        failures = {'FAILED', 'CANCELED', 'STOPPED', 'SKIPPED', 'INTERRUPTED', 'NOT_STARTED'}
        for item in state:
            with self.subTest(state=item):
                self.assertTrue(matches(item, 0))
                self.assertEqual(matches(item, 1), item == state.COMPLETED)
                self.assertEqual(matches(item, 2), item.name in failures)
        self.assertEqual(state.CANCELED.value, 'Canceled')

    def test_stored_canceled_status_uses_american_spelling(self):
        path = ROOT / 'src/objects/project/project_build.py'
        tree = ast.parse(path.read_text())
        status = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'StageBuildStatus')
        namespace = {'Enum': Enum}
        exec(compile(ast.Module(body=[status], type_ignores=[]), str(path), 'exec'), namespace)
        cls = namespace['StageBuildStatus']
        self.assertEqual(cls.CANCELED.value, 'canceled')
        with self.assertRaises(ValueError):
            cls('cancelled')
