"""Build machine workspace placement and cleanup."""
from pathlib import Path
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
    def test_workspace_image_is_under_library_temporary(self):
        repository = types.ModuleType('catalystlab.repository')
        repository.library_path = lambda *names: os.path.join('/library', *names)
        with patch.dict(sys.modules, {'catalystlab.repository': repository}):
            self.assertEqual(lima.workspace_image_path('catalystlab-00000000'),
                             '/library/Temporary/BuildMachines/catalystlab-00000000/workspace.img')
            self.assertEqual(os.path.dirname(lima.workspace_image_path('catalystlab-00000000')),
                             os.path.dirname(lima.temporary_swap_disk_path('catalystlab-00000000-swap')))

    def test_previous_temporary_workspace_path_remains_available_for_cleanup(self):
        repository = types.ModuleType('catalystlab.repository')
        repository.library_path = lambda *names: os.path.join('/library', *names)
        with patch.dict(sys.modules, {'catalystlab.repository': repository}):
            self.assertEqual(lima.previous_temporary_workspace_image_path('catalystlab-00000000'),
                             '/library/Temporary/Workspaces/catalystlab-00000000.img')

    def test_legacy_workspace_path_remains_available_for_cleanup(self):
        with patch('catalystlab.lima.machines_directory', return_value='/library/Machines'):
            self.assertEqual(lima.legacy_workspace_image_path('catalystlab-00000000'),
                             '/library/Machines/workspaces/catalystlab-00000000.img')

    def test_swap_disk_data_moves_to_temporary_and_lima_path_becomes_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            home = os.path.join(root, 'Machines')
            managed = os.path.join(home, '_disks', 'catalystlab-test-swap')
            temporary = os.path.join(root, 'Temporary', 'BuildMachines', 'catalystlab-test', 'swap-disk')
            os.makedirs(managed)
            Path(managed, 'datadisk').write_text('raw disk')
            with patch('catalystlab.lima.temporary_swap_disk_path', return_value=os.path.realpath(temporary)), \
                    patch('catalystlab.lima.previous_temporary_swap_disk_path', return_value=os.path.join(root, 'OldSwapDisk')), \
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
