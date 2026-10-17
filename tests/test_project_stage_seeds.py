from pathlib import Path
import sys
import types
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.project_stage import load_stage_possible_seeds


class ProjectStageSeedTests(unittest.TestCase):
    def test_excludes_selected_stage_and_descendants_but_keeps_parent_and_siblings(self):
        parent, selected, child, grandchild, sibling = [
            types.SimpleNamespace(id=uuid.uuid4()) for _ in range(5)
        ]
        project = types.SimpleNamespace(
            stages=[parent, selected, child, grandchild, sibling],
            stages_tree=lambda: [types.SimpleNamespace(value=parent, children=[
                types.SimpleNamespace(value=selected, children=[
                    types.SimpleNamespace(value=child, children=[
                        types.SimpleNamespace(value=grandchild, children=[]),
                    ]),
                ]),
                types.SimpleNamespace(value=sibling, children=[]),
            ])],
        )

        self.assertEqual(load_stage_possible_seeds(selected, project), [parent, sibling])

    def test_returns_every_other_stage_when_selected_stage_is_missing_from_tree(self):
        first, second, missing = [types.SimpleNamespace(id=uuid.uuid4()) for _ in range(3)]
        project = types.SimpleNamespace(stages=[first, second], stages_tree=lambda: [])

        self.assertEqual(load_stage_possible_seeds(missing, project), [first, second])


if __name__ == '__main__':
    unittest.main()
