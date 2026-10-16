"""Progress parsing, archive support, and deployment package planning."""
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

from catalystlab.deploy_boot import Bootloader, Kernel, StageContents
from catalystlab.deploy_installation import (
    DeployInstallation, DeployRepository, DeploySystemSettings, DeployUser,
    EmergeProgress, tar_extract_command,
)
from catalystlab.deploy_system import NetworkService, NetworkSettings, WifiSettings
from catalystlab.deploy_target import PartitionPlan, TargetDisk, TargetMachine


class EmergeProgressTests(unittest.TestCase):
    def test_emerging_and_completed_package_lines_report_monotonic_progress(self):
        progress = EmergeProgress()
        self.assertAlmostEqual(progress.parse('>>> Emerging (2 of 10) category/pkg-1'), 0.1)
        self.assertIsNone(progress.parse('Jobs: 0 of 10 complete'))
        self.assertAlmostEqual(progress.parse('Jobs: 3 of 10 complete'), 0.3)
        self.assertIsNone(progress.parse('>>> Emerging (3 of 10) next'), 'Starting work does not move completion')
        self.assertAlmostEqual(progress.parse('>>> Completed (4 of 10) next'), 0.4)
        self.assertAlmostEqual(progress.parse('\x1b[32m>>> Emerging (1 of 3) new emerge\x1b[0m'), 0.0)
        self.assertIsNone(progress.parse('ordinary output'))

    def test_progress_handles_carriage_return_updates_and_new_emerge_total(self):
        progress = EmergeProgress()
        self.assertAlmostEqual(progress.parse('Jobs: 2 of 5 complete\rJobs: 4 of 5 complete'), 0.8)
        self.assertIsNone(progress.parse('Jobs: 3 of 5 complete'))
        self.assertAlmostEqual(progress.parse('>>> Completed binary (1 of 2) cat/pkg'), 0.5)


class ArchiveAndUserTests(unittest.TestCase):
    def test_tar_extract_command_selects_supported_compression_and_quotes_target(self):
        cases = {
            'stage.tar.xz': '-J', 'stage.tar.zst': '--zstd', 'stage.tar.zstd': '--zstd',
            'stage.tar.bz2': '-j', 'stage.tar.gz': '-z', 'stage.tar.lz4': '--use-compress-program=lz4',
            'stage.tar.lzma': '--lzma', 'stage.tar': '',
        }
        for archive, compression in cases.items():
            with self.subTest(archive=archive):
                command = tar_extract_command(archive, '/mnt/a disk')
                self.assertIn(compression, command)
                self.assertIn("-C '/mnt/a disk'", command)
                self.assertIn('--numeric-owner', command)
                self.assertIn('--xattrs-include', command)
        with self.assertRaisesRegex(RuntimeError, 'Unsupported archive'):
            tar_extract_command('stage.zip')

    def test_deploy_user_gecos_replaces_account_field_delimiters(self):
        self.assertEqual(DeployUser('alice', '', ' Alice, Example: Admin ').gecos, 'Alice  Example  Admin')


class PackagePlanningTests(unittest.TestCase):
    def installation(self, settings, contents, local=False, image_path=None):
        instance = object.__new__(DeployInstallation)
        instance.build = types.SimpleNamespace(stage_name='stage4', path=None, artifact_path='/build/stage.tar.xz')
        instance.project_name = 'test'
        instance.project_id = None
        instance.connection = types.SimpleNamespace(host='machine')
        instance.machine = TargetMachine('aarch64', False, 0, 'machine', local=local)
        instance.plan = PartitionPlan(TargetDisk('/dev/sda', 32 * 1024**3), False,
                                      image_path=image_path, compress_image=True)
        instance.settings = settings
        instance.contents = contents
        instance.output_path = None
        return instance

    def test_packages_only_include_missing_services_bootloader_firmware_and_kernel(self):
        settings = DeploySystemSettings(
            bootloader=Bootloader.GRUB,
            kernel=Kernel.DISTRIBUTION_BINARY,
            install_firmware=True,
            network=NetworkSettings(NetworkService.DHCPCD),
            wifi=WifiSettings('wifi'),
        )
        contents = StageContents(bootloaders={Bootloader.GRUB}, network_services={NetworkService.DHCPCD},
                                wpa_supplicant=True, firmware=True)
        instance = self.installation(settings, contents)
        self.assertEqual(instance.packages, [Kernel.DISTRIBUTION_BINARY.package])

    def test_packages_add_network_support_wpa_firmware_and_missing_bootloader(self):
        settings = DeploySystemSettings(
            bootloader=Bootloader.SYSTEMD_BOOT,
            kernel=Kernel.RASPBERRY_PI,
            install_firmware=True,
            network=NetworkSettings(NetworkService.NETIFRC),
            wifi=WifiSettings('wifi'),
        )
        instance = self.installation(settings, StageContents(init='openrc'))
        self.assertEqual(instance.packages, [
            'sys-apps/systemd-utils', 'net-wireless/wpa_supplicant',
            'sys-kernel/linux-firmware', Kernel.RASPBERRY_PI.package,
        ])

    def test_deployment_name_and_image_output_paths(self):
        instance = self.installation(DeploySystemSettings(), StageContents(), image_path='/tmp/test.img')
        self.assertEqual(instance.target_name, 'test.img.xz')
        self.assertEqual(instance.created_image_path, '/tmp/test.img.xz')
        self.assertEqual(instance.result_path, '/tmp/test.img.xz')
        instance.output_path = '/chosen/output.img.xz'
        self.assertEqual(instance.result_path, '/chosen/output.img.xz')
        disk = self.installation(DeploySystemSettings(), StageContents())
        self.assertEqual(disk.target_name, 'machine')
        disk.machine.local = True
        self.assertEqual(disk.target_name, '/dev/sda')


class DeploymentPlanTests(unittest.TestCase):
    def installation(self, *, image=False, compress=False, repository=DeployRepository.NONE,
                     bootloader=Bootloader.NONE, network=NetworkService.NONE, kernel=Kernel.NONE):
        plan = types.SimpleNamespace(
            image=image, compress_image=compress, image_path='/temporary/test.img', root='/mnt/gentoo',
            disk=types.SimpleNamespace(path='/dev/sda'),
        )
        settings = DeploySystemSettings(repository=repository, bootloader=bootloader, kernel=kernel,
                                         network=NetworkSettings(network))
        build = types.SimpleNamespace(stage_name='stage4', path=None, artifact_path='/build/stage4.tar.xz')
        machine = TargetMachine('arm64', False, 0, 'builder')
        return DeployInstallation(build, 'project', Mock(), machine, plan, settings, StageContents())

    def test_disk_installation_schedules_only_required_steps_in_order(self):
        process = self.installation()
        self.assertEqual([stage.name for stage in process.stages], [
            'Check machine', 'Partition disk', 'Format partitions', 'Mount partitions',
            'Install stage', 'Configure system', 'Finish',
        ])

    def test_optional_repository_package_network_and_bootloader_steps_are_included(self):
        process = self.installation(repository=DeployRepository.LATEST, bootloader=Bootloader.GRUB,
                                    network=NetworkService.DHCPCD, kernel=Kernel.DISTRIBUTION_BINARY)
        self.assertEqual([stage.name for stage in process.stages], [
            'Check machine', 'Partition disk', 'Format partitions', 'Mount partitions',
            'Install stage', 'Configure system', 'Install Gentoo repository', 'Install packages',
            'Configure network', 'Install bootloader', 'Finish',
        ])

    def test_image_plan_adds_image_growth_shrink_compress_and_save_steps(self):
        process = self.installation(image=True, compress=True)
        self.assertEqual([stage.name for stage in process.stages], [
            'Create image', 'Partition disk', 'Format partitions', 'Mount partitions',
            'Install stage', 'Configure system', 'Grow at first start', 'Finish',
            'Shrink image', 'Compress image', 'Save image',
        ])

    def test_uncompressed_image_skips_compress_step(self):
        process = self.installation(image=True, compress=False)
        self.assertNotIn('Compress image', [stage.name for stage in process.stages])
        self.assertEqual(process.created_image_path, '/temporary/test.img')

    def test_failed_image_deployment_detaches_and_removes_incomplete_outputs(self):
        process = self.installation(image=True, compress=True)
        with patch('catalystlab.deploy_image.detach_image_script', return_value='detach'):
            process.complete_process(False)
        script = process.connection.run.call_args.args[0]
        self.assertIn('detach', script)
        self.assertIn('rm -f', script)
        self.assertIn('/temporary/test.img', script)
        self.assertIn('/temporary/test.img.xz', script)
        process.connection.close.assert_called_once_with()

    def test_local_disk_cleanup_unmounts_after_success(self):
        process = self.installation()
        process.machine.local = True
        process.mounted = True
        process.complete_process(True)
        script = process.connection.run.call_args.args[0]
        self.assertIn('umount -R -l /mnt/gentoo', script)
        self.assertFalse(process.mounted)
        process.connection.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
