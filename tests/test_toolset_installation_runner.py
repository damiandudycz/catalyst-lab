"""Toolset installation uses its operation's runner without storing it on the toolset."""
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
from catalystlab.toolset_installation import ToolsetInstallationStepSpawn


class FakeToolset:
    def __init__(self, env, identifier, name, metadata=None, squashfs_binding_dir=None):
        self.env = env
        self.uuid = identifier
        self.name = name
        self.metadata = metadata if metadata is not None else {}
        self.squashfs_binding_dir = squashfs_binding_dir
        self.reserve = Mock(return_value=True)
        self.spawn = Mock()


class ToolsetInstallationRunnerTests(unittest.TestCase):
    def test_spawn_step_passes_operation_runner_to_toolset_spawn(self):
        runner = object()
        process = types.SimpleNamespace(
            name=lambda: "test toolset",
            tmp_stage_extract_dir="/temporary/stage",
            stage_url=types.SimpleNamespace(geturl=lambda: "https://example.invalid/stage.tar.xz"),
            allow_binpkgs=True,
            runner=runner,
            _continue_process=Mock(),
        )
        step = ToolsetInstallationStepSpawn(process)
        step.run_command_in_toolset = Mock(return_value=True)

        with patch("catalystlab.toolset_installation.Toolset", FakeToolset), \
                patch("catalystlab.multistage_process.GLib.idle_add"):
            step.start()

        self.assertEqual(step.state, MultiStageProcessStageState.COMPLETED)
        self.assertEqual(process.toolset.squashfs_binding_dir, "/temporary/stage")
        process.toolset.spawn.assert_called_once_with(runner=runner, store_changes=True)
        self.assertEqual(step.run_command_in_toolset.call_count, 2)


if __name__ == '__main__':
    unittest.main()
