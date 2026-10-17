from pathlib import Path
import sys
import types
import unittest
import uuid
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.toolset import Toolset


class ToolsetRootOverlayTests(unittest.TestCase):
    def test_writable_root_overlay_is_applied_before_device_and_proc_mounts(self):
        toolset = object.__new__(Toolset)
        toolset.work_dir = '/temporary/session'
        toolset.root_overlay_options = [
            '--overlay-src', '/toolset', '--overlay', '/temporary/upper', '/temporary/work', '/',
        ]
        toolset.bind_options = ['--ro-bind', '/snapshot', '/var/db/repos/gentoo']

        command = toolset._bwrap_script('emerge --pretend')

        self.assertLess(command.index('--overlay-src'), command.index('--dev /dev'))
        self.assertLess(command.index('--dev /dev'), command.index('--ro-bind'))
        self.assertIn('--overlay /temporary/upper /temporary/work /', command)

    def test_release_unmounts_each_read_only_machine_source_and_clears_state(self):
        for mounted_attribute in ('machine_cache_mounted', 'squashfs_loop_mounted'):
            with self.subTest(mounted_attribute=mounted_attribute):
                toolset = object.__new__(Toolset)
                toolset.rootless = True
                machine, executor = object(), Mock()
                toolset.machine_id = uuid.uuid4()
                toolset.squashfs_binding_dir = '/temporary/toolset'
                toolset.machine_cache_mounted = mounted_attribute == 'machine_cache_mounted'
                toolset.squashfs_loop_mounted = mounted_attribute == 'squashfs_loop_mounted'
                toolset.rootless_writable_copy = None

                with patch('catalystlab.build_machine.build_machine_for_id', return_value=machine), \
                        patch('catalystlab.toolset.executor_for_machine', return_value=executor), \
                        patch('catalystlab.toolset.unmount_read_only_mount') as unmount:
                    toolset._release_squashfs_binding_dir()

                unmount.assert_called_once_with('/temporary/toolset', print, executor=executor)
                self.assertFalse(toolset.machine_cache_mounted)
                self.assertFalse(toolset.squashfs_loop_mounted)
                self.assertIsNone(toolset.squashfs_binding_dir)


if __name__ == '__main__':
    unittest.main()
