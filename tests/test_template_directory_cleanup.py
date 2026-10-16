"""Removing a template stage must also remove its empty directory hierarchy."""
import ast
from pathlib import Path
import os
import tempfile
import unittest


class TemplateDirectoryCleanupTests(unittest.TestCase):
    def test_nested_empty_stage_directories_are_removed(self):
        path = Path(__file__).resolve().parents[1] / 'src/objects/project/project_template_update.py'
        tree = ast.parse(path.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'apply_changes')
        namespace = dict(os=os, _STAGES_DIRECTORY='stages', _STAGE_FILE='stage.json')
        exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), function], type_ignores=[])), str(path), 'exec'), namespace)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'stages'
            (root / 'removed/root_overlay/etc/X11').mkdir(parents=True)
            (root / 'kept').mkdir()
            (root / 'kept/stage.json').write_text('{}')
            # Untracked/user files must not be lost while cleaning empty directories.
            (root / 'custom').mkdir()
            (root / 'custom/notes').write_text('keep')
            namespace['apply_changes'](temporary, [], None, None)
            self.assertFalse((root / 'removed').exists())
            self.assertTrue((root / 'kept/stage.json').is_file())
            self.assertEqual((root / 'custom/notes').read_text(), 'keep')
