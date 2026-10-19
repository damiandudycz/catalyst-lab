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

    def test_kvm_warning_is_shown_when_device_cannot_be_opened(self):
        with patch.object(lima.sys, 'platform', 'linux'), \
             patch.object(lima.platform, 'machine', return_value='aarch64'), \
             patch.object(lima.os, 'open', side_effect=PermissionError):
            warning = lima.kvm_access_warning()

        self.assertIn('/dev/kvm', warning)
        self.assertIn('kvm group', warning)

    def test_kvm_warning_is_hidden_when_device_is_accessible(self):
        with patch.object(lima.sys, 'platform', 'linux'), \
             patch.object(lima.platform, 'machine', return_value='aarch64'), \
             patch.object(lima.os, 'open', return_value=42), \
             patch.object(lima.os, 'close') as close:
            warning = lima.kvm_access_warning()

        self.assertIsNone(warning)
        close.assert_called_once_with(42)

    def test_lima_environment_forces_tcg_when_kvm_device_is_inaccessible(self):
        with patch.object(lima.sys, 'platform', 'linux'), \
             patch.object(lima.platform, 'machine', return_value='aarch64'), \
             patch.object(lima.os, 'open', side_effect=PermissionError), \
             patch.object(lima.os, 'makedirs'), \
             patch.dict(lima.os.environ, {}, clear=True):
            environment = lima.lima_environment('/tmp/lima-test')

        self.assertEqual(environment['QEMU_SYSTEM_AARCH64'],
                         'qemu-system-aarch64 -machine virt,accel=tcg -cpu max')

    def test_lima_environment_preserves_custom_qemu_override(self):
        custom = 'my-qemu-wrapper'
        with patch.object(lima.sys, 'platform', 'linux'), \
             patch.object(lima.platform, 'machine', return_value='aarch64'), \
             patch.object(lima.os, 'open', side_effect=PermissionError), \
             patch.object(lima.os, 'makedirs'), \
             patch.dict(lima.os.environ, {'QEMU_SYSTEM_AARCH64': custom}, clear=True):
            environment = lima.lima_environment('/tmp/lima-test')

        self.assertEqual(environment['QEMU_SYSTEM_AARCH64'], custom)

    def test_qemu_9p_warning_when_device_is_not_available(self):
        result = types.SimpleNamespace(returncode=0, stdout='name "virtio-blk-pci"', stderr='')
        with patch.object(lima.sys, 'platform', 'linux'), \
             patch.object(lima.platform, 'machine', return_value='aarch64'), \
             patch.object(lima.shutil, 'which', return_value='/usr/bin/qemu-system-aarch64'), \
             patch.object(lima.subprocess, 'run', return_value=result):
            warning = lima.qemu_9p_unavailable_warning()

        self.assertIn('virtio-9p-pci', warning)

    def test_qemu_system_warning_when_native_emulator_is_missing(self):
        with patch.object(lima.sys, 'platform', 'linux'), \
             patch.object(lima.platform, 'machine', return_value='aarch64'), \
             patch.object(lima.shutil, 'which', return_value=None):
            warning = lima.qemu_system_unavailable_warning()

        self.assertIn('qemu-system-aarch64', warning)

    def test_qemu_system_warning_is_hidden_when_native_emulator_exists(self):
        with patch.object(lima.sys, 'platform', 'linux'), \
             patch.object(lima.platform, 'machine', return_value='aarch64'), \
             patch.object(lima.shutil, 'which', return_value='/usr/bin/qemu-system-aarch64'):
            warning = lima.qemu_system_unavailable_warning()

        self.assertIsNone(warning)

    def test_linux_discovers_private_gentoo_lima_binary(self):
        private_path = '/usr/libexec/catalyst-lab/lima/bin/limactl'
        with patch.object(lima.sys, 'platform', 'linux'), \
             patch.object(lima.shutil, 'which', return_value='/usr/bin/limactl'), \
             patch.object(lima.os, 'access', side_effect=lambda path, _mode: path == private_path):
            self.assertEqual(lima.limactl_path(), private_path)


if __name__ == '__main__':
    unittest.main()
