"""Exercise row construction with a worker finishing before subscriptions exist."""
import ast
from enum import Enum, auto
from pathlib import Path
import types
import unittest
from unittest.mock import Mock


class State(Enum):
    SCHEDULED = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELED = auto()


class RowStub:
    def __init__(self, **kwargs):
        self.set_sensitive = Mock()
        self.set_subtitle = Mock()
        self.add_suffix = Mock()
        self.add_prefix = Mock()
        self.connect = Mock()
        self.set_activatable = Mock()


class InstallerCompletionTests(unittest.TestCase):
    def test_completion_during_row_construction_is_displayed(self):
        path = Path(__file__).resolve().parents[1] / 'src/ui/components/multistage_process_execution_view/multistage_process_execution_view.py'
        tree = ast.parse(path.read_text())
        row = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'MultiStageProcessStageRow')
        # Load the actual row implementation without requiring a display or compiled GTK resources.
        namespace = {
            'Adw': types.SimpleNamespace(ActionRow=RowStub),
            'Gtk': types.SimpleNamespace(Label=Mock, Image=Mock(new_from_icon_name=Mock(return_value=Mock()))),
            'MultiStageProcessStageState': State,
            'MultiStageProcessStageEvent': types.SimpleNamespace(STATE_CHANGED=1, PROGRESS_CHANGED=2, OUTPUT_LINE_ADDED=3),
        }
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), row], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
        for initial_state in (State.SCHEDULED, State.IN_PROGRESS):
            with self.subTest(initial_state=initial_state):
                step = types.SimpleNamespace(name='Cached seed', description='Download seed', state=initial_state,
                                             progress=None, output_lines=[], event_bus=Mock())
                def subscribe(*args):
                    # The worker finishes before its completion observer is registered.
                    step.state = State.COMPLETED
                    step.progress = 1.0
                step.event_bus.subscribe.side_effect = subscribe
                view = namespace['MultiStageProcessStageRow'](step, Mock())
                view.progress_label.set_label.assert_called_with('100%')
                view.status_icon.set_from_icon_name.assert_called_with('check-square-svgrepo-com-symbolic')
                view.status_icon.add_css_class.assert_called_with('success')
                view.set_sensitive.assert_called_with(True)


if __name__ == '__main__':
    unittest.main()
