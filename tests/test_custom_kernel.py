"""Custom VM kernel package selection and safely generated Portage command."""
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
import json
import shlex
import tarfile
import io
import gzip
import struct

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.custom_kernel import (copy_kernel_outputs, kernel_package_build_command,
                                       portage_config_setup_command,
                                       valid_kernel_package,
                                       KERNEL_OUTPUTS_MANIFEST, KERNEL_BUILD_METADATA,
                                       write_kernel_build_metadata)


class CustomKernelTests(unittest.TestCase):
    def test_kernel_build_metadata_is_saved_as_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata = {
                'built_at': '2026-10-18T10:00:00+00:00',
                'architecture': 'arm64',
                'kernel_package': 'sys-kernel/gentoo-kernel-bin',
                'toolset': {'uuid': 'toolset-id', 'name': 'arm64 openrc'},
                'gentoo_snapshot': 'gentoo-abc.sqfs',
                'emerge_arguments': ['--oneshot', 'sys-kernel/gentoo-kernel-bin'],
            }
            path = write_kernel_build_metadata(temporary, metadata)
            self.assertEqual(Path(path).name, KERNEL_BUILD_METADATA)
            with open(path, encoding='utf-8') as file:
                saved = json.load(file)
        self.assertEqual(saved['schema_version'], 1)
        self.assertEqual(saved['built_at'], metadata['built_at'])
        self.assertEqual(saved['toolset'], metadata['toolset'])
        self.assertEqual(saved['gentoo_snapshot'], metadata['gentoo_snapshot'])

    def test_portage_config_is_merged_into_writable_run_mount(self):
        with tempfile.TemporaryDirectory() as temporary:
            base, stage, destination = (Path(temporary, name) for name in ("base", "stage", "portage"))
            (base / "package.use").mkdir(parents=True)
            (base / "package.use" / "base").write_text("base setting")
            (base / "gnupg").mkdir()
            (base / "gnupg" / "private-key").write_text("secret")
            (stage / "package.use").mkdir(parents=True)
            (stage / "package.use" / "stage").write_text("stage setting")
            command = (portage_config_setup_command()
                       .replace("/var/tmp/catalystlab-base-portage", str(base))
                       .replace("/var/tmp/catalystlab-stage-portage", str(stage))
                       .replace("/etc/portage", str(destination)))

            subprocess.run(["bash", "-e", "-c", command], check=True)

            self.assertEqual((destination / "package.use" / "base").read_text(), "base setting")
            self.assertEqual((destination / "package.use" / "stage").read_text(), "stage setting")
            self.assertFalse((destination / "gnupg").exists())

    def test_copy_kernel_outputs_preserves_module_archive_with_case_collisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, 'cache')
            destination = Path(temporary, 'temporary')
            source.mkdir()
            (source / 'custom-kernel').write_bytes(b'kernel')
            (source / 'custom-initramfs').write_bytes(b'initramfs')
            archive_path = source / 'custom-modules.tar'
            with tarfile.open(archive_path, 'w') as archive:
                for name, contents in (('6.18.57/kernel/net/netfilter/xt_RATEEST.ko', b'upper'),
                                       ('6.18.57/kernel/net/netfilter/xt_rateest.ko', b'lower')):
                    entry = tarfile.TarInfo(name)
                    entry.size = len(contents)
                    archive.addfile(entry, io.BytesIO(contents))

            copy_kernel_outputs(str(source), str(destination))

            self.assertEqual((destination / 'custom-kernel').read_bytes(), b'kernel')
            self.assertEqual((destination / 'custom-initramfs').read_bytes(), b'initramfs')
            with tarfile.open(destination / 'custom-modules.tar') as archive:
                self.assertEqual(archive.getnames(), [
                    '6.18.57/kernel/net/netfilter/xt_RATEEST.ko',
                    '6.18.57/kernel/net/netfilter/xt_rateest.ko',
                ])

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
        self.assertIn('custom-modules.tar', command)
        self.assertIn(f'rm -f /var/tmp/catalystlab-kernel-output/{KERNEL_OUTPUTS_MANIFEST}', command)
        self.assertIn(f'> /var/tmp/catalystlab-kernel-output/{KERNEL_OUTPUTS_MANIFEST}', command)
        self.assertIn('tar -cf /var/tmp/catalystlab-kernel-output/custom-modules.tar', command)
        self.assertIn('generating one with dracut', command)
        self.assertIn('dracut --force --no-hostonly --kver', command)
        self.assertLess(command.index('rm -f /var/tmp/catalystlab-kernel-output/'),
                        command.index('emerge --ask=n --oneshot'))
        self.assertGreater(command.index('> /var/tmp/catalystlab-kernel-output/' + KERNEL_OUTPUTS_MANIFEST),
                           command.index('custom-modules.tar -C /lib/modules'))

    def test_kernel_selection_is_shared_with_all_backends(self):
        command = kernel_package_build_command('sys-kernel/gentoo-kernel-bin')
        raw_image = 'build_kernel="$release/build/arch/arm64/boot/Image"'
        self.assertIn("find /boot -maxdepth 1 -type f", command)
        self.assertIn("-name 'vmlinuz*'", command)
        self.assertIn(raw_image, command)
        self.assertLess(command.index(raw_image), command.index('find /boot'))
        self.assertIn('Using raw ARM64 Linux Image from the matching kernel build tree', command)
        self.assertIn('No raw ARM64 Image found in the matching build tree; using /boot kernel', command)
        self.assertIn('is_arm64_image', command)
        self.assertNotIn('skip=56', command)

    def test_kernel_selection_prefers_matching_raw_image(self):
        command = kernel_package_build_command('sys-kernel/gentoo-kernel')
        selection = command.split('release=$(find ', 1)[1].split('\ninitramfs=', 1)[0]
        selection = 'release=$(find ' + selection
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            modules = root / 'lib/modules/6.18.57-gentoo'
            source = root / 'usr/src/linux/arch/arm64/boot'
            boot = root / 'boot'
            modules.mkdir(parents=True)
            source.mkdir(parents=True)
            boot.mkdir()
            (modules / 'build').symlink_to(root / 'usr/src/linux')
            raw_image = source / 'Image'
            raw_image.write_bytes(b'raw Image')
            (boot / 'vmlinuz-6.18.57').write_bytes(b'EFI/PE image')
            script = (selection.replace('find /lib/modules ', f'find {root / "lib/modules"} ')
                      .replace('find /boot ', f'find {boot} ').replace('sort -V', 'sort'))

            result = subprocess.run(['bash', '-e', '-c', script + '\nprintf "SELECTED:%s\\n" "$kernel"'],
                                    check=True, capture_output=True, text=True)
            self.assertIn(f'SELECTED:{modules / "build/arch/arm64/boot/Image"}', result.stdout)

    def test_kernel_selection_falls_back_to_boot_for_binary_packages(self):
        command = kernel_package_build_command('sys-kernel/gentoo-kernel-bin')
        selection = command.split('release=$(find ', 1)[1].split('\ninitramfs=', 1)[0]
        selection = 'release=$(find ' + selection
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            modules = root / 'lib/modules/6.18.57-gentoo-dist-bin'
            boot = root / 'boot'
            modules.mkdir(parents=True)
            boot.mkdir()
            binary_kernel = boot / 'vmlinuz.efi'
            binary_kernel.write_bytes(b'EFI_ZBOOT binary image')
            script = (selection.replace('find /lib/modules ', f'find {root / "lib/modules"} ')
                      .replace('find /boot ', f'find {boot} ').replace('sort -V', 'sort'))

            result = subprocess.run(['bash', '-e', '-c', script + '\nprintf "SELECTED:%s\\n" "$kernel"'],
                                    check=True, capture_output=True, text=True)

            self.assertIn(f'SELECTED:{binary_kernel}', result.stdout)

    def test_build_command_extracts_raw_image_from_efi_zboot_bin_kernel(self):
        command = kernel_package_build_command('sys-kernel/gentoo-kernel-bin')
        script = command.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        raw_image = bytearray(4096)
        struct.pack_into('<Q', raw_image, 16, len(raw_image))
        raw_image[56:60] = b'ARM\x64'
        compressed = gzip.compress(raw_image)

        with tempfile.TemporaryDirectory() as temporary:
            source, destination = Path(temporary, 'vmlinuz'), Path(temporary, 'custom-kernel')
            pe = bytearray(0x100 + len(compressed))
            struct.pack_into('<I', pe, 0x3c, 0x40)
            pe[0x40:0x44] = b'PE\\0\\0'
            struct.pack_into('<H', pe, 0x46, 1)  # one section
            struct.pack_into('<H', pe, 0x54, 0)  # no optional header
            section = 0x58
            pe[section:section + 8] = b'.rodata'
            struct.pack_into('<II', pe, section + 16, len(compressed), 0x100)
            pe[0x100:] = compressed
            source.write_bytes(pe)

            subprocess.run([sys.executable, '-c', script, str(source), str(destination)], check=True)

            self.assertEqual(destination.read_bytes(), raw_image)

    def test_build_command_keeps_an_already_raw_kernel_image(self):
        command = kernel_package_build_command('sys-kernel/gentoo-kernel-bin')
        script = command.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        image = bytearray(4096)
        struct.pack_into('<Q', image, 16, len(image))
        image[56:60] = b'ARM\x64'
        with tempfile.TemporaryDirectory() as temporary:
            source, destination = Path(temporary, 'Image'), Path(temporary, 'custom-kernel')
            source.write_bytes(image)
            subprocess.run([sys.executable, '-c', script, str(source), str(destination)], check=True)
            self.assertEqual(destination.read_bytes(), image)

    def test_build_command_extracts_zstd_efi_zboot_payload(self):
        command = kernel_package_build_command('sys-kernel/gentoo-kernel-bin')
        script = command.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        image = bytearray(4096)
        struct.pack_into('<Q', image, 16, len(image))
        image[56:60] = b'ARM\x64'
        compressed = subprocess.run(['zstd', '-q', '-c'], input=image, check=True,
                                    stdout=subprocess.PIPE).stdout + struct.pack('<I', len(image))

        with tempfile.TemporaryDirectory() as temporary:
            source, destination = Path(temporary, 'vmlinuz'), Path(temporary, 'custom-kernel')
            pe = bytearray(0x100 + len(compressed))
            struct.pack_into('<I', pe, 0x3c, 0x40)
            pe[0x40:0x44] = b'PE\0\0'
            struct.pack_into('<H', pe, 0x46, 1)
            struct.pack_into('<H', pe, 0x54, 0)
            section = 0x58
            pe[section:section + 8] = b'.rodata'
            struct.pack_into('<II', pe, section + 16, len(compressed), 0x100)
            pe[0x100:] = compressed
            source.write_bytes(pe)

            subprocess.run([sys.executable, '-c', script, str(source), str(destination)], check=True)
            self.assertEqual(destination.read_bytes(), image)

    def test_build_command_uses_existing_binary_packages_when_requested(self):
        command = kernel_package_build_command('sys-kernel/gentoo-kernel', use_binary_packages=True)
        self.assertIn('emerge --ask=n --oneshot --verbose --usepkg sys-kernel/gentoo-kernel', command)
        self.assertIn('emerge --ask=n --oneshot --verbose --usepkg sys-kernel/dracut', command)


if __name__ == '__main__':
    unittest.main()
