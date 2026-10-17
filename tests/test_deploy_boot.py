"""Boot detection and generated boot setup scripts."""
import gzip
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

from catalystlab.deploy_boot import (
    Bootloader, Kernel, StageContents, bootloader_script, bootloader_warnings,
    default_bootloader, grub_platform, grub_target, kboot_script,
    packages_script, raspberry_pi_script,
)
from catalystlab.deploy_system import NetworkService


class BootloaderRulesTests(unittest.TestCase):
    def test_package_names_and_configuration_only_bootloaders(self):
        self.assertEqual(Bootloader.SYSTEMD_BOOT.package('systemd'), 'sys-apps/systemd')
        self.assertEqual(Bootloader.SYSTEMD_BOOT.package('openrc'), 'sys-apps/systemd-utils')
        self.assertEqual(Bootloader.RASPBERRY_PI.package(None), 'sys-boot/raspberrypi-firmware')
        self.assertIsNone(Bootloader.KBOOT.package('openrc'))
        self.assertFalse(Bootloader.NONE.needs_package)
        self.assertEqual(Bootloader.RASPBERRY_PI.configuration_files, ['config.txt', 'cmdline.txt'])

    def test_support_matrix_accounts_for_firmware_and_removable_disks(self):
        self.assertTrue(Bootloader.GRUB.supported('x86_64', False))
        self.assertFalse(Bootloader.GRUB.supported('aarch64', False))
        self.assertTrue(Bootloader.SYSTEMD_BOOT.supported('aarch64', True))
        self.assertFalse(Bootloader.SYSTEMD_BOOT.supported('aarch64', False))
        self.assertFalse(Bootloader.EFI_STUB.supported('x86_64', True, removable=True))
        self.assertTrue(Bootloader.KBOOT.supported('ppc64', False))
        self.assertFalse(Bootloader.KBOOT.supported('ppc64', True))
        self.assertTrue(Bootloader.RASPBERRY_PI.supported('aarch64', False))
        self.assertFalse(Bootloader.RASPBERRY_PI.supported('x86_64', False))
        self.assertTrue(Bootloader.NONE.supported('unknown', False))

    def test_kernel_package_and_architecture_support(self):
        self.assertEqual(Kernel.DISTRIBUTION_BINARY.package, 'sys-kernel/gentoo-kernel-bin')
        self.assertEqual(Kernel.RASPBERRY_PI.package, 'sys-kernel/raspberrypi-image')
        self.assertIsNone(Kernel.STAGE.package)
        self.assertTrue(Kernel.RASPBERRY_PI.supported('armv7l'))
        self.assertFalse(Kernel.RASPBERRY_PI.supported('x86_64'))

    def test_stage_contents_detects_init_services_boot_and_package_groups(self):
        contents = StageContents()
        contents._add_path('usr/lib/systemd/systemd')
        contents._add_path('sbin/init', '/usr/bin/openrc-init')
        contents._add_path('usr/bin/sshd')
        contents._add_path('etc/runlevels/default/sshd', '/etc/init.d/sshd')
        contents._add_path('etc/systemd/system/multi-user.target.wants/ssh.service', '/dev/null')
        contents._add_path('etc/systemd/system/sockets.target.wants/sshd.socket')
        contents._add_path('usr/sbin/grub-install')
        contents._add_path('boot/vmlinuz-6.1')
        contents._add_path('boot/initramfs-6.1.img')
        contents._add_path('var/db/pkg/acct-group/render-0/')
        contents._add_path('var/db/pkg/sys-kernel/linux-firmware-20240101/')
        contents._add_path('var/db/pkg/sys-firmware/raspberrypi-wifi-ucode-1/')
        contents._add_path('usr/sbin/openrc-run')

        self.assertEqual(contents.init, 'openrc')
        self.assertTrue(contents.ssh_installed)
        self.assertTrue(contents.ssh_enabled)
        self.assertEqual(contents.bootloaders, {Bootloader.GRUB})
        self.assertEqual(contents.kernels, ['vmlinuz-6.1'])
        self.assertEqual(contents.initramfs, ['initramfs-6.1.img'])
        self.assertEqual(contents.network_services, set())
        self.assertTrue(contents.firmware)
        self.assertEqual(contents.firmware_conflict, 'sys-firmware/raspberrypi-wifi-ucode')
        self.assertIn('render', contents.groups)
        self.assertIn('kernel vmlinuz-6.1', contents.summary)

    def test_stage_contents_ignores_disabled_services_and_detects_network_packages(self):
        contents = StageContents()
        contents._add_path('usr/lib/systemd/systemd')
        contents._add_path('usr/sbin/sshd')
        contents._add_path('usr/lib/systemd/system/sshd.service')
        contents._add_path('etc/systemd/system/multi-user.target.wants/sshd.service', '/dev/null')
        contents._add_path('usr/bin/NetworkManager')
        contents._add_path('usr/bin/dhcpcd')
        contents._add_path('etc/init.d/net.lo')
        contents._add_path('usr/sbin/wpa_supplicant')
        self.assertFalse(contents.ssh_enabled)
        self.assertTrue(contents.ssh_installed)
        self.assertEqual(contents.network_services, {
            NetworkService.NETWORK_MANAGER, NetworkService.DHCPCD, NetworkService.NETIFRC})
        self.assertTrue(contents.wpa_supplicant)

    def test_scanner_uses_gzip_listing_and_falls_back_when_listing_is_unrecognized(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'stage.tar.xz'
            archive.touch()
            with gzip.open(str(archive) + '.CONTENTS.gz', 'wt') as listing:
                listing.write('-rwxr-xr-x root/root 12 2026-01-01 00:00 usr/bin/sshd\n')
                listing.write('-rwxr-xr-x root/root 12 2026-01-01 00:00 boot/Image\n')
            result = StageContents.scan(str(archive))
            self.assertTrue(result.ssh_installed)
            self.assertEqual(result.kernels, ['Image'])

            Path(str(archive) + '.CONTENTS.gz').write_bytes(gzip.compress(b'not a tar listing\n'))
            process = Mock()
            process.stdout.__enter__ = Mock(return_value=iter([
                '-rwxr-xr-x root/root 12 2026-01-01 00:00 usr/sbin/grub-install\n']))
            process.stdout.__exit__ = Mock(return_value=False)
            process.wait.return_value = 0
            with patch('catalystlab.deploy_boot.subprocess.Popen', return_value=process):
                fallback = StageContents.scan(str(archive), [])
            self.assertEqual(fallback.bootloaders, {Bootloader.GRUB})

    def test_scanner_reports_archive_listing_failure(self):
        process = Mock()
        process.stdout.__enter__ = Mock(return_value=iter([]))
        process.stdout.__exit__ = Mock(return_value=False)
        process.wait.return_value = 2
        with tempfile.TemporaryDirectory() as directory, patch('catalystlab.deploy_boot.subprocess.Popen', return_value=process):
            with self.assertRaisesRegex(RuntimeError, 'Failed to list stage archive'):
                StageContents.scan(str(Path(directory) / 'missing.tar.xz'))

    def test_default_bootloader_uses_supported_stage_choice_then_fallback(self):
        self.assertEqual(default_bootloader(StageContents(bootloaders={Bootloader.SYSTEMD_BOOT, Bootloader.GRUB}),
                                            'x86_64', True), Bootloader.GRUB)
        self.assertEqual(default_bootloader(StageContents(bootloaders={Bootloader.SYSTEMD_BOOT}),
                                            'aarch64', True), Bootloader.SYSTEMD_BOOT)
        self.assertEqual(default_bootloader(StageContents(), 'ppc64', False), Bootloader.KBOOT)
        self.assertEqual(default_bootloader(StageContents(), 'aarch64', False, raspberry_pi=True), Bootloader.RASPBERRY_PI)
        self.assertEqual(default_bootloader(StageContents(), 'aarch64', False), Bootloader.NONE)

    def test_warnings_cover_missing_kernel_and_removable_uefi_boot(self):
        warnings = bootloader_warnings(Bootloader.GRUB, True, True, False)
        self.assertEqual([item.summary for item in warnings], ['No kernel to boot', 'No firmware boot entry'])
        self.assertIn('menu will be empty', warnings[0].details)
        self.assertEqual(bootloader_warnings(Bootloader.NONE, True, True, False), [])


class BootScriptTests(unittest.TestCase):
    def test_grub_platform_mapping_and_unknowns(self):
        self.assertEqual(grub_target('x86_64', False), 'i386-pc')
        self.assertEqual(grub_target('aarch64', True), 'arm64-efi')
        self.assertIsNone(grub_target('ppc64', False))
        self.assertEqual(grub_platform('i686', True), 'efi-32')
        self.assertEqual(grub_platform('aarch64', True), 'efi-64')
        self.assertIsNone(grub_platform('aarch64', False))

    def test_package_setup_adds_init_kernel_firmware_and_boot_files_configuration(self):
        script = packages_script(['sys-apps/systemd', 'sys-kernel/gentoo-kernel-bin',
                                  'sys-kernel/linux-firmware', 'sys-boot/raspberrypi-firmware'],
                                 'efi-64', generic_initramfs=True,
                                 bootloader=Bootloader.RASPBERRY_PI)
        self.assertIn('GRUB_PLATFORMS="efi-64"', script)
        self.assertIn('sys-apps/systemd boot kernel-install', script)
        self.assertIn('sys-kernel/installkernel dracut', script)
        self.assertIn('hostonly="no"', script)
        self.assertIn('sys-kernel/linux-firmware linux-fw-redistributable', script)
        self.assertIn('mv "$ROOT/boot/$FILE"', script)
        self.assertIn('for FILE in config.txt cmdline.txt', script)
        self.assertIn('restore_boot_files', script)
        self.assertIn('emerge --noreplace --quiet-build=y', script)

    def test_package_setup_configures_the_systemd_package_actually_selected(self):
        script = packages_script(['sys-apps/systemd-utils'], None)

        self.assertIn('sys-apps/systemd-utils boot kernel-install', script)
        self.assertNotIn('sys-apps/systemd boot kernel-install', script)

    def test_grub_install_script_adds_rootwait_and_removable_fallback(self):
        script = bootloader_script(Bootloader.GRUB, 'x86_64', True, '/dev/sda', '/dev/sda2',
                                   '/dev/sda1', '/efi', removable=True)
        self.assertIn('grub-install --target=x86_64-efi --efi-directory=/efi --bootloader-id=Gentoo --removable --no-nvram', script)
        self.assertIn('GRUB_CMDLINE_LINUX="${GRUB_CMDLINE_LINUX} rootwait"', script)
        self.assertIn('grub-mkconfig', script)
        self.assertIsNone(bootloader_script(Bootloader.GRUB, 'aarch64', False, '/dev/sda', '/dev/sda2', None, None))

    def test_systemd_boot_refind_and_stub_reference_root_and_copy_kernel(self):
        systemd = bootloader_script(Bootloader.SYSTEMD_BOOT, 'aarch64', True, '/dev/nvme0n1',
                                    '/dev/nvme0n1p2', '/dev/nvme0n1p1', '/efi', removable=True)
        self.assertIn('bootctl install --esp-path=/efi --no-variables', systemd)
        self.assertIn('root=UUID=$(blkid -s UUID -o value /dev/nvme0n1p2) rw rootwait', systemd)
        self.assertIn('cp "$ROOT/boot/$KERNEL" "$ROOT/efi/gentoo/linux"', systemd)
        refind = bootloader_script(Bootloader.REFIND, 'x86_64', True, '/dev/sda', '/dev/sda2', '/dev/sda1', '/efi')
        self.assertIn('mount --bind "$ROOT/efi" "$ROOT/boot/efi"', refind)
        self.assertIn('refind-install --yes', refind)
        stub = bootloader_script(Bootloader.EFI_STUB, 'x86_64', True, '/dev/nvme0n1',
                                 '/dev/nvme0n1p2', '/dev/nvme0n1p1', '/efi')
        self.assertIn('--part 1', stub)
        self.assertIn('OPTIONS="$OPTIONS initrd=', stub)
        self.assertIsNone(bootloader_script(Bootloader.SYSTEMD_BOOT, 'aarch64', True, '/dev/sda', '/dev/sda2', None, None))

    def test_kboot_supports_separate_boot_partuuid_and_ps3_framebuffer(self):
        script = kboot_script('/dev/sda2', separate_boot=True, ps3=True, by_partuuid=True)
        self.assertIn('CONFIG="$ROOT/boot/kboot.conf"', script)
        self.assertIn('PARTUUID=$(blkid -s PARTUUID -o value /dev/sda2)', script)
        self.assertIn('video=ps3fb:mode:133', script)
        self.assertIn('initrd=/$INITRD', script)
        fallback = kboot_script('/dev/sda2', separate_boot=False, ps3=None)
        self.assertIn('grep -qi "PS3"', fallback)
        self.assertIn('CONFIG="$ROOT/etc/kboot.conf"', fallback)

    def test_raspberry_pi_script_preserves_existing_config_and_writes_rootwait(self):
        arm = raspberry_pi_script('/dev/mmcblk0p2', 'aarch64')
        self.assertIn('arm_64bit=1', arm)
        self.assertIn('root=PARTUUID=$PARTUUID', arm)
        self.assertIn('rootwait', arm)
        self.assertIn('[ ! -f "$ROOT/boot/config.txt" ]', arm)
        self.assertNotIn('arm_64bit=1', raspberry_pi_script('/dev/sda2', 'armv7l'))


if __name__ == '__main__':
    unittest.main()
