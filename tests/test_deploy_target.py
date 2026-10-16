"""Disk discovery and partition plans used by the deploy workflow."""
from pathlib import Path
import json
import sys
import types
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.deploy_target import (
    GIB, MIB, MBR_MAX_SIZE, PartitionPlan, PartitionSpec, PartitionTable,
    PartitionType, TargetDisk, TargetFirmware, TargetMachine, TargetFilesystem,
    architecture_supported, default_image_layout, default_layout,
)


class TargetMachineTests(unittest.TestCase):
    def test_remote_discovery_ignores_non_writable_and_non_disk_devices(self):
        connection = Mock()
        connection.output.return_value = "\n".join((
            'architecture=aarch64', 'firmware=uefi', 'memory_kib=4194304',
            'hostname=pi', 'model=Raspberry Pi 5',
            'disks=' + json.dumps({'blockdevices': [
                {'name': 'sda', 'path': '/dev/sda', 'size': 1000, 'type': 'disk', 'ro': '0', 'rm': '1', 'tran': 'usb', 'model': 'Stick'},
                {'name': 'loop0', 'path': '/dev/loop0', 'size': 2000, 'type': 'loop', 'ro': '0'},
                {'name': 'sdb', 'path': '/dev/sdb', 'size': 3000, 'type': 'disk', 'ro': '1'},
                {'name': 'sdc', 'path': '/dev/sdc', 'size': 0, 'type': 'disk', 'ro': '0'},
            ]}),
            'interface=eth0', 'address=192.0.2.2/24', 'gateway=192.0.2.1', 'dns=192.0.2.53',
        ))

        machine = TargetMachine.load(connection)

        self.assertEqual((machine.architecture, machine.memory, machine.hostname), ('aarch64', 4 * GIB, 'pi'))
        self.assertEqual((machine.interface, machine.address, machine.gateway, machine.dns),
                         ('eth0', '192.0.2.2/24', '192.0.2.1', '192.0.2.53'))
        self.assertEqual([disk.path for disk in machine.disks], ['/dev/sda'])
        self.assertTrue(machine.disks[0].external)
        self.assertTrue(machine.disks[0].removable)

    def test_local_discovery_marks_system_mounts_in_use_and_orders_external_first(self):
        connection = Mock(host='host')
        connection.output.return_value = json.dumps({'blockdevices': [
            {'name': 'disk0', 'path': '/dev/disk0', 'size': 100 * GIB, 'type': 'disk', 'ro': '0', 'tran': 'nvme',
             'children': [{'name': 'disk0p1', 'type': 'part', 'mountpoints': ['/']} ]},
            {'name': 'disk2', 'path': '/dev/disk2', 'size': 32 * GIB, 'type': 'disk', 'ro': '0', 'rm': '1', 'tran': 'usb',
             'children': [{'name': 'disk2p1', 'type': 'part', 'mountpoints': ['/run/media/user/SD']} ]},
            {'name': 'zram0', 'path': '/dev/zram0', 'size': GIB, 'type': 'disk', 'ro': '0'},
            {'name': 'disk3', 'path': '/dev/disk3', 'size': 64 * GIB, 'type': 'disk', 'ro': '0', 'tran': 'sata'},
        ]})

        machine = TargetMachine.load_local(connection, 'aarch64', TargetFirmware.UEFI)

        self.assertEqual([disk.path for disk in machine.disks], ['/dev/disk2', '/dev/disk3', '/dev/disk0'])
        system, removable, unused = machine.disks[2], machine.disks[0], machine.disks[1]
        self.assertTrue(system.in_use)
        self.assertEqual(system.in_use_reason, 'in use, mounted at /')
        self.assertFalse(removable.in_use)
        self.assertTrue(removable.automounted)
        self.assertFalse(unused.external)

    def test_remote_disk_mountpoints_are_detected_recursively(self):
        connection = Mock()
        connection.output.return_value = 'disks=' + json.dumps({'blockdevices': [
            {'name': 'sda', 'size': GIB, 'type': 'disk', 'children': [
                {'name': 'sda1', 'type': 'part', 'mountpoints': ['/mnt/root']},
            ]},
        ]})
        self.assertTrue(TargetMachine.load(connection).disks[0].in_use)


class PartitionPlanTests(unittest.TestCase):
    def disk(self, size=128 * GIB, path='/dev/sda'):
        return TargetDisk(path, size)

    def test_image_layout_keeps_root_last_and_uses_firmware_partition(self):
        layout = default_image_layout(False, 'aarch64', raspberry_pi=True)
        self.assertEqual([(item.type, item.mount_point) for item in layout], [
            (PartitionType.FIRMWARE, '/boot'), (PartitionType.LINUX, '/')])
        plan = PartitionPlan(self.disk(), False, layout, raspberry_pi=True, image_path='/tmp/image.img')
        self.assertIsNone(plan.error())
        self.assertEqual(plan.root_index, 2)
        self.assertEqual(plan.partitions[0].filesystem, 'vfat')
        self.assertEqual(plan.partitions[1].filesystem, 'ext4')

    def test_image_rejects_non_ext4_or_non_final_growable_root(self):
        firmware = PartitionSpec(PartitionType.FIRMWARE, 512 * MIB, mount_point='/boot')
        fixed_root = PartitionSpec(PartitionType.LINUX, 20 * GIB, TargetFilesystem.EXT4, '/')
        plan = PartitionPlan(self.disk(), False, [firmware, fixed_root], raspberry_pi=True, image_path='/tmp/image.img')
        self.assertIn('Root partition of image must be last', plan.error())

    def test_uefi_requires_one_valid_efi_partition(self):
        root = PartitionSpec(PartitionType.LINUX, None, TargetFilesystem.EXT4, '/')
        missing = PartitionPlan(self.disk(), True, [root])
        self.assertEqual(missing.error(), 'UEFI machine needs EFI system partition')
        efi = PartitionSpec(PartitionType.EFI, 32 * MIB, mount_point='/efi')
        small = PartitionPlan(self.disk(), True, [efi, root])
        self.assertEqual(small.error(), 'EFI system partition needs at least 64 MiB')

    def test_partition_validation_rejects_duplicate_mountpoints_and_early_remainder(self):
        root = PartitionSpec(PartitionType.LINUX, 20 * GIB, TargetFilesystem.EXT4, '/')
        efi = PartitionSpec(PartitionType.EFI, 512 * MIB, mount_point='/')
        self.assertEqual(PartitionPlan(self.disk(), False, [root, efi]).error(), 'Mount points must be different')
        remainder = PartitionSpec(PartitionType.LINUX, None, TargetFilesystem.EXT4, '/')
        home = PartitionSpec(PartitionType.LINUX, 10 * GIB, TargetFilesystem.EXT4, '/home')
        self.assertEqual(PartitionPlan(self.disk(), False, [remainder, home]).error(),
                         'Only last partition can use remaining space')

    def test_default_layout_caps_swap_and_splits_large_disk(self):
        layout = default_layout(self.disk(512 * GIB), True, 64 * GIB, 'aarch64')
        swap = next(part for part in layout if part.type == PartitionType.SWAP)
        self.assertEqual(swap.size, 8 * GIB)
        self.assertEqual(layout[-2].mount_point, '/')
        self.assertEqual(layout[-1].mount_point, '/home')
        self.assertEqual(layout[-1].size, None)

    def test_mbr_size_limit_and_partition_device_names(self):
        plan = PartitionPlan(self.disk(3 * 1024 * GIB, '/dev/nvme0n1'), False,
                             default_image_layout(False, 'aarch64'), table=PartitionTable.MBR)
        self.assertEqual(plan.usable_size, MBR_MAX_SIZE - 4 * MIB)
        self.assertEqual(plan.partition_device(1), '/dev/nvme0n1p1')
        self.assertTrue(any('MBR uses only first' in warning for warning in plan.warnings('aarch64')))

    def test_mount_script_mounts_parent_before_nested_mount(self):
        layout = [
            PartitionSpec(PartitionType.LINUX, 1 * GIB, TargetFilesystem.EXT4, '/home/user'),
            PartitionSpec(PartitionType.LINUX, None, TargetFilesystem.EXT4, '/'),
            PartitionSpec(PartitionType.EFI, 512 * MIB, mount_point='/efi'),
        ]
        plan = PartitionPlan(self.disk(), True, layout)
        script = plan.mount_script()
        self.assertLess(script.index('Mounting /dev/sda2 (/)'), script.index('Mounting /dev/sda3 (/efi)'))
        self.assertLess(script.index('Mounting /dev/sda3 (/efi)'), script.index('Mounting /dev/sda1 (/home/user)'))

    def test_format_script_uses_safe_filesystem_labels_and_device_numbers(self):
        plan = PartitionPlan(self.disk(path='/dev/mmcblk0'), False,
            [PartitionSpec(PartitionType.FIRMWARE, 512 * MIB, mount_point='/boot'),
             PartitionSpec(PartitionType.LINUX, None, TargetFilesystem.EXT4, '/')],
            raspberry_pi=True)
        script = plan.format_script()
        self.assertIn('mkfs.vfat -F 32 -n BOOT /dev/mmcblk0p1', script)
        self.assertIn('mkfs.ext4 -F -L root /dev/mmcblk0p2', script)


class ArchitectureSupportTests(unittest.TestCase):
    def test_build_architecture_compatibility_includes_emulated_modes(self):
        self.assertTrue(architecture_supported('arm', 'aarch64'))
        self.assertTrue(architecture_supported('x86', 'x86_64'))
        self.assertFalse(architecture_supported('arm64', 'x86_64'))
        self.assertIsNone(architecture_supported('unknown', 'aarch64'))


if __name__ == '__main__':
    unittest.main()
