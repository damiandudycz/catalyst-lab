import unittest

from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.tree_node import TreeNode, walk_tree


class TreeNodeTests(unittest.TestCase):
    def test_walk_yields_preorder_nodes_with_depth(self):
        root, left, right, leaf = map(TreeNode, "abcd")
        root.children = [left, right]
        left.children = [leaf]

        self.assertEqual([(node.value, depth) for node, depth in walk_tree([root])],
                         [("a", 0), ("b", 1), ("d", 2), ("c", 1)])

    def test_walk_accepts_starting_depth(self):
        root = TreeNode("root")
        root.children.append(TreeNode("child"))

        self.assertEqual([(node.value, depth) for node, depth in walk_tree([root], 3)],
                         [("root", 3), ("child", 4)])


if __name__ == "__main__":
    unittest.main()
