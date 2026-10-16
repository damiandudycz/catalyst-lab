"""Tree warnings must never become selectable build-plan entries."""
import ast
from pathlib import Path
import types
import unittest
from unittest.mock import Mock
import uuid


class BuildStageSelectionTests(unittest.TestCase):
    def test_broken_stages_and_missing_parent_warnings_are_not_selectable(self):
        path = Path(__file__).resolve().parents[1] / 'src/ui/project/project_build_view.py'
        tree = ast.parse(path.read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        load = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'load_stages')
        gtk, adw = Mock(), Mock()
        namespace = dict(Gtk=gtk, Adw=adw, GLib=types.SimpleNamespace(markup_escape_text=lambda s: s),
                         stage_target_icon=Mock(return_value='stage-symbolic'))
        exec(compile(ast.Module(body=[load], type_ignores=[]), str(path), 'exec'), namespace)
        broken = types.SimpleNamespace(id=uuid.uuid4(), name='Broken', is_broken=True,
                                       warning_message='Could not load stage')
        missing = types.SimpleNamespace(id=uuid.uuid4(), name='?', is_broken=True,
                                        warning_message='Parent stage was not found')
        valid = types.SimpleNamespace(id=uuid.uuid4(), name='stage4', target='stage4')
        view = types.SimpleNamespace(_stages_in_tree_order=lambda: [(broken, 0), (missing, 0), (valid, 1)],
                                     rows={}, stages_group=Mock(), on_stage_toggled=Mock())
        namespace['load_stages'](view)
        self.assertEqual(set(view.rows), {valid.id})
        self.assertEqual(view.stages_group.add.call_count, 3)
        gtk.CheckButton.assert_called_once()
        self.assertEqual(adw.ActionRow.return_value.set_subtitle.call_count, 2)
