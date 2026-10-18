"""Toolset image creation publishes only a complete SquashFS artifact."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.multistage_process import MultiStageProcessStageState
from catalystlab.toolset_installation import ToolsetInstallationStepCompress


class FakeSquashfsProcess:
    def __init__(self, write_output=True, return_code=0):
        self.stdout = iter(['Creating 4.0 filesystem\n', '100\n'])
        self.output_path = None
        self.write_output = write_output
        self.return_code = return_code

    def wait(self):
        if self.write_output:
            Path(self.output_path).write_bytes(b'squashfs image')
        return self.return_code


class ToolsetCompressionTests(unittest.TestCase):
    def _step(self, root, process):
        cache = Path(root, 'Cache')
        final = Path(root, 'Toolsets', 'arm64_openrc', 'toolset.sqfs')
        final.parent.mkdir(parents=True)
        toolset = types.SimpleNamespace(
            file_path=lambda: str(final),
            create_squashfs=lambda output_file: self._create_squashfs(process, output_file),
            unspawn=Mock(),
        )
        executor = types.SimpleNamespace(cache_directory=lambda: str(cache))
        multistage_process = types.SimpleNamespace(
            toolset=toolset, executor=executor, _continue_process=Mock()
        )
        return ToolsetInstallationStepCompress(multistage_process), final

    @staticmethod
    def _create_squashfs(process, output_file):
        process.output_path = output_file
        return process

    def test_writes_unique_temporary_image_to_shared_cache_then_publishes_it(self):
        with tempfile.TemporaryDirectory() as root:
            process = FakeSquashfsProcess()
            step, final = self._step(root, process)

            with patch('catalystlab.multistage_process.GLib.idle_add'):
                step.start()

            self.assertEqual(step.state, MultiStageProcessStageState.COMPLETED)
            self.assertEqual(final.read_bytes(), b'squashfs image')
            self.assertEqual(Path(process.output_path).parent, Path(root, 'Cache', 'ToolsetBuilds'))
            self.assertNotEqual(process.output_path, str(final) + '.tmp')
            self.assertFalse(Path(process.output_path).name.startswith('.'))
            self.assertIn('Creating 4.0 filesystem', step.output_lines)
            self.assertFalse(Path(process.output_path).exists())

    def test_missing_output_reports_failure_without_deleting_existing_toolset(self):
        with tempfile.TemporaryDirectory() as root:
            process = FakeSquashfsProcess(write_output=False)
            step, final = self._step(root, process)
            final.write_bytes(b'previous valid image')

            with patch('catalystlab.multistage_process.GLib.idle_add'):
                step.start()
                step.cleanup()

            self.assertEqual(step.state, MultiStageProcessStageState.FAILED)
            self.assertEqual(final.read_bytes(), b'previous valid image')
            self.assertTrue(any('completed without creating its output file' in line for line in step.output_lines))


if __name__ == '__main__':
    unittest.main()
