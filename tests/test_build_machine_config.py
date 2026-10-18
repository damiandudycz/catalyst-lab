"""Lima configuration compatibility checks."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import lima


class VMRunnerConfigTests(unittest.TestCase):
    def test_generated_config_does_not_set_deprecated_top_level_rosetta(self):
        configuration = lima.machine_configuration(4, 8, ['/library/Cache'])

        self.assertNotIn('\nrosetta:\n', configuration)
        self.assertIn('base:\n- template:_images/alpine-3.23', configuration)

    def test_linux_uses_qemu_and_9p_mounts(self):
        with patch.object(lima.sys, 'platform', 'linux'):
            configuration = lima.machine_configuration(4, 8, ['/library/Cache'])

        self.assertIn('vmType: qemu', configuration)
        self.assertIn('mountType: 9p', configuration)

    def test_macos_uses_virtualization_framework_defaults(self):
        with patch.object(lima.sys, 'platform', 'darwin'):
            configuration = lima.machine_configuration(4, 8, ['/library/Cache'])

        self.assertIn('vmType: vz', configuration)
        self.assertNotIn('mountType:', configuration)

    def test_vm_support_depends_on_lima_availability(self):
        with patch.object(lima, 'limactl_path', return_value='/usr/bin/limactl'):
            self.assertTrue(lima.virtual_machines_supported())
        with patch.object(lima, 'limactl_path', return_value=None):
            self.assertFalse(lima.virtual_machines_supported())


if __name__ == '__main__':
    unittest.main()
