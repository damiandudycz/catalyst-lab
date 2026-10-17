"""Custom VM kernel package selection and safely generated Portage command."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.custom_kernel import (copy_kernel_outputs, is_arm64_linux_image,
                                       kernel_package_build_command, valid_kernel_package)


class CustomKernelTests(unittest.TestCase):
    def test_copy_kernel_outputs_preserves_dangling_module_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, 'cache')
            destination = Path(temporary, 'temporary')
            modules = source / 'custom-modules' / '6.18.57-gentoo-dist-bin'
            modules.mkdir(parents=True)
            (source / 'custom-kernel').write_bytes(b'kernel')
            (source / 'custom-initramfs').write_bytes(b'initramfs')
            (modules / 'xt_example.ko').write_bytes(b'module')
            (modules / 'source').symlink_to('/usr/src/linux-6.18.57')

            copy_kernel_outputs(str(source), str(destination))

            self.assertEqual((destination / 'custom-kernel').read_bytes(), b'kernel')
            self.assertEqual((destination / 'custom-initramfs').read_bytes(), b'initramfs')
            self.assertEqual((destination / 'custom-modules/6.18.57-gentoo-dist-bin/xt_example.ko').read_bytes(),
                             b'module')
            self.assertTrue((destination / 'custom-modules/6.18.57-gentoo-dist-bin/source').is_symlink())

    def test_arm64_image_detection_distinguishes_raw_image_from_pe_efi_kernel(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary, 'Image')
            raw.write_bytes(b'\0' * 0x38 + b'ARM\x64')
            efi = Path(temporary, 'vmlinuz.efi')
            efi.write_bytes(b'MZ' + b'\0' * 0x36 + b'PE\0\0')
            self.assertTrue(is_arm64_linux_image(str(raw)))
            self.assertFalse(is_arm64_linux_image(str(efi)))


    def test_accepts_portage_atoms_with_optional_repository(self):
        self.assertTrue(valid_kernel_package('sys-kernel/gentoo-kernel'))
        self.assertTrue(valid_kernel_package('sys-kernel/custom-kernel::my-overlay'))
        self.assertTrue(valid_kernel_package('sys-kernel/foo_bar+experimental'))

    def test_rejects_shell_syntax_and_incomplete_atoms(self):
        for package in ('', 'gentoo-kernel', '/gentoo-kernel', 'sys-kernel/',
                        'sys-kernel/foo;touch /tmp/pwned', 'sys-kernel/foo --pretend'):
            with self.subTest(package=package):
                self.assertFalse(valid_kernel_package(package))
        with self.assertRaises(ValueError):
            kernel_package_build_command('sys-kernel/foo; false')

    def test_build_command_emerges_package_and_exports_kernel_files_and_modules(self):
        command = kernel_package_build_command('sys-kernel/gentoo-kernel::gentoo')
        self.assertIn('emerge --ask=n --oneshot --verbose', command)
        self.assertIn('sys-kernel/installkernel dracut', command)
        self.assertIn('/etc/portage/package.use/99-catalystlab-test-vm-kernel', command)
        self.assertIn('sys-kernel/gentoo-kernel::gentoo', command)
        self.assertIn('/boot', command)
        self.assertIn('custom-kernel', command)
        self.assertIn('custom-initramfs', command)
        self.assertIn('custom-modules', command)
        self.assertIn('rm -rf /mnt/catalystlab-kernel-output/custom-modules', command)
        self.assertIn('generating one with dracut', command)
        self.assertIn('dracut --force --no-hostonly --kver', command)

    def test_apple_kernel_command_selects_raw_arm64_image_and_explains_bin_package_limit(self):
        command = kernel_package_build_command('sys-kernel/gentoo-kernel-bin', require_arm64_image=True)
        self.assertIn("-path '*/arch/arm64/boot/Image'", command)
        self.assertIn('Apple Virtualization requires a raw ARM64 Image', command)
        self.assertNotIn("-name 'vmlinuz*'", command)

    def test_build_command_uses_existing_binary_packages_when_requested(self):
        command = kernel_package_build_command('sys-kernel/gentoo-kernel', use_binary_packages=True)
        self.assertIn('emerge --ask=n --oneshot --verbose --usepkg sys-kernel/gentoo-kernel', command)
        self.assertIn('emerge --ask=n --oneshot --verbose --usepkg sys-kernel/dracut', command)


if __name__ == '__main__':
    unittest.main()
