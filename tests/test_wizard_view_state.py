"""Cover wizard state updates that can happen during page construction."""
import ast
from pathlib import Path
import types
import unittest


class WizardViewStateTests(unittest.TestCase):
    def test_refresh_buttons_is_safe_without_a_displayed_page(self):
        path = Path(__file__).resolve().parents[1] / "src/ui/components/wizard/wizard_view.py"
        tree = ast.parse(path.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WizardView")
        refresh = next(node for node in cls.body
                       if isinstance(node, ast.FunctionDef) and node.name == "_refresh_buttons_state")
        namespace = {}
        exec(compile(ast.Module(body=[refresh], type_ignores=[]), str(path), "exec"), namespace)

        for pages, current_page in (([], 0), ([object()], 1)):
            with self.subTest(pages=len(pages), current_page=current_page):
                view = types.SimpleNamespace(pages=pages, current_page=current_page)
                namespace["_refresh_buttons_state"](view)


if __name__ == "__main__":
    unittest.main()
