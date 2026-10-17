class TreeNode:
    """A value with child nodes, shared by project models and their tree view."""

    def __init__(self, value):
        self.value = value
        self.children = []


def walk_tree(roots, depth=0):
    """Yield tree nodes with depth, in display order."""
    pending = [(node, depth) for node in reversed(roots)]
    while pending:
        node, node_depth = pending.pop()
        yield node, node_depth
        pending.extend((child, node_depth + 1) for child in reversed(node.children))
