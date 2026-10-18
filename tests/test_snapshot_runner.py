"""Snapshot generation uses the runner selected for the operation."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.multistage_process import MultiStageProcessStageState
from catalystlab.snapshot_installation import SnapshotInstallationStepPrepareToolset


class SnapshotRunnerTests(unittest.TestCase):
    def test_prepare_step_spawns_toolset_on_selected_runner(self):
        runner = object()
        toolset = types.SimpleNamespace(
            spawned=False,
            in_use=False,
            reserve=Mock(return_value=True),
            spawn=Mock(),
            unspawn=Mock(),
        )
        process = types.SimpleNamespace(
            toolset=toolset,
            runner=runner,
            reused_snapshot=False,
            _continue_process=Mock(),
        )
        step = SnapshotInstallationStepPrepareToolset(toolset, process)
        step.required_bindings = Mock(return_value=[])

        with patch("catalystlab.multistage_process.GLib.idle_add"):
            step.start()

        self.assertEqual(step.state, MultiStageProcessStageState.COMPLETED)
        toolset.spawn.assert_called_once_with(runner=runner, additional_bindings=[])
        self.assertTrue(step.unspawn)


if __name__ == '__main__':
    unittest.main()
