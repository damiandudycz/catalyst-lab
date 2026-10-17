class TreeNode:
    """A value with child nodes, shared by project models and their tree view."""

    def __init__(self, value):
        self.value = value
        self.children = []
