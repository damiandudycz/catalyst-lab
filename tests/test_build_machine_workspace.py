"""Build machine workspace placement and cleanup."""
from pathlib import Path
import subprocess
import sys
import types
import os
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import lima


class BuildMachineWorkspaceTests(unittest.TestCase):
    def test_empty_workspace_cleanup_stops_at_first_nonempty_directory(self):
        with tempfile.TemporaryDirectory() as root:
            run_directory = os.path.join(root, 'BuildMachines', 'catalystlab-test')
            os.makedirs(run_directory)
            with patch('catalystlab.lima.build_machine_temporary_directory', return_value=run_directory):
                lima.remove_empty_build_machine_temporary_directory('catalystlab-test')
                self.assertFalse(os.path.exists(os.path.dirname(run_directory)))

                os.makedirs(run_directory)
                Path(run_directory, 'swap-disk').mkdir()
                lima.remove_empty_build_machine_temporary_directory('catalystlab-test')

            self.assertTrue(os.path.isdir(run_directory))
            self.assertTrue(os.path.isdir(os.path.dirname(run_directory)))

    def test_build_machine_model_import_does_not_load_gtk(self):
        package_paths = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
        code = (
            'import sys, types; '
            'package = types.ModuleType("catalystlab"); '
            f'package.__path__ = {package_paths!r}; '
            'sys.modules["catalystlab"] = package; '
            'import catalystlab.build_machine; '
            'assert "gi.repository.Gtk" not in sys.modules'
        )
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_workspace_image_is_under_library_temporary(self):
        repository = types.ModuleType('catalystlab.repository')
        repository.library_path = lambda *names: os.path.join('/library', *names)
        repository.resolve_path = lambda path: os.path.realpath(os.path.expanduser(path))
        with patch.dict(sys.modules, {'catalystlab.repository': repository}):
            self.assertEqual(lima.workspace_image_path('catalystlab-00000000'),
                             '/library/Temporary/BuildMachines/catalystlab-00000000/workspace.img')
            self.assertEqual(os.path.dirname(lima.workspace_image_path('catalystlab-00000000')),
                             os.path.dirname(lima.temporary_swap_disk_path('catalystlab-00000000-swap')))

    def test_swap_disk_data_moves_to_temporary_and_lima_path_becomes_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            home = os.path.join(root, 'Machines')
            managed = os.path.join(home, '_disks', 'catalystlab-test-swap')
            temporary = os.path.join(root, 'Temporary', 'BuildMachines', 'catalystlab-test', 'swap-disk')
            os.makedirs(managed)
            Path(managed, 'datadisk').write_text('raw disk')
            with patch('catalystlab.lima.temporary_swap_disk_path', return_value=os.path.realpath(temporary)), \
                    patch('catalystlab.lima.remove_empty_build_machine_temporary_directory'):
                result = lima.move_lima_disk_to_temporary('catalystlab-test-swap', home)

                self.assertEqual(result, managed)
                self.assertTrue(os.path.islink(managed))
                self.assertEqual(os.path.realpath(managed), os.path.realpath(temporary))
                self.assertEqual(Path(temporary, 'datadisk').read_text(), 'raw disk')

                lima.remove_temporary_lima_disk_data('catalystlab-test-swap', home)

            self.assertFalse(os.path.lexists(managed))
            self.assertFalse(os.path.lexists(temporary))


if __name__ == '__main__':
    unittest.main()
