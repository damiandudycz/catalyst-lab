"""Validate image sizing and generated disk-image setup scripts without root access."""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import deploy_image


class ImageSizingTests(unittest.TestCase):
    def test_initial_image_size_has_eight_gib_minimum(self):
        self.assertEqual(deploy_image.initial_image_size(0), 8 * 1024**3)
        self.assertEqual(deploy_image.initial_image_size(1024**3), 16 * 1024**3)

    def test_initial_image_size_scales_archive_and_rounds_to_gib(self):
        size = deploy_image.initial_image_size(3 * 1024**3)
        self.assertEqual(size, 32 * 1024**3)
        self.assertGreaterEqual(size, 8 * 1024**3 + 8 * 3 * 1024**3)

    def test_image_path_uses_temporary_location_and_sanitizes_stage_name(self):
        from catalystlab.repository import Repository
        with tempfile.TemporaryDirectory() as temporary:
            settings = types.SimpleNamespace(value=types.SimpleNamespace(temporary_location=temporary))
            with patch.object(Repository, 'Settings', settings, create=True):
                path = deploy_image.image_path(types.SimpleNamespace(stage_name='../stage/4'))
        self.assertTrue(Path(path).is_relative_to(Path(temporary).resolve()))
        self.assertIn('.._stage_4-', Path(path).name)


class ImageScriptTests(unittest.TestCase):
    def test_create_script_makes_sparse_image_and_attaches_loop_partitions(self):
        script = deploy_image.create_image_script('/tmp/a image.img', 9 * 1024**3)
        self.assertIn('mkdir -p', script)
        self.assertIn('truncate -s 9663676416', script)
        self.assertIn('losetup -f --show -P', script)
        self.assertIn('LOOP=$LOOP', script)

    def test_detach_script_detaches_all_image_loop_devices_and_is_best_effort(self):
        script = deploy_image.detach_image_script('/tmp/a image.img')
        self.assertIn('losetup -j', script)
        self.assertIn('losetup -d "$LOOP"', script)
        self.assertTrue(script.rstrip().endswith('true'))

    def test_shrink_script_resizes_root_and_updates_partition(self):
        script = deploy_image.shrink_image_script('/tmp/image.img', '/dev/loop0', '/dev/loop0p2', 2, False)
        for expected in ('e2fsck -fy', 'resize2fs -M', 'dumpe2fs -h', 'sfdisk --no-reread',
                         'losetup -d', 'truncate -s $END'):
            with self.subTest(expected=expected):
                self.assertIn(expected, script)
        self.assertIn('FREE=$((USED * 20 / 100))', script)
        self.assertIn('FREE=268435456', script)
        self.assertNotIn('last-lba', script)

    def test_gpt_shrink_script_rewrites_table_for_new_image_size(self):
        script = deploy_image.shrink_image_script('/tmp/image.img', '/dev/loop0', '/dev/loop0p2', 2, True)
        self.assertIn('sfdisk -d', script)
        self.assertIn('last-lba', script)
        self.assertIn('echo "$TABLE" | sfdisk --no-reread', script)

    def test_compress_script_replaces_old_archive_and_uses_all_cores(self):
        script = deploy_image.compress_image_script('/tmp/image.img')
        self.assertIn('rm -f /tmp/image.img.xz', script)
        self.assertIn('xz -T0 -6 /tmp/image.img', script)
        self.assertIn('du -h /tmp/image.img.xz', script)

    def test_grow_root_program_expands_last_partition_and_removes_oneshot_service(self):
        program = deploy_image._GROW_ROOT_PROGRAM
        self.assertIn('sfdisk --relocate gpt-bak-std', program)
        self.assertIn('sfdisk --no-reread --force -N "$NUMBER"', program)
        self.assertIn('resize2fs "$PART"', program)
        self.assertIn('catalystlab-grow-root.service', program)
        self.assertIn('rm -f /etc/local.d/catalystlab-grow-root.start', program)

    def test_grow_root_installer_creates_openrc_and_systemd_variants(self):
        script = deploy_image.grow_root_script()
        self.assertIn('CL_INIT_SYSTEM', script)
        self.assertIn('etc/local.d', script)
        self.assertIn('etc/systemd/system/multi-user.target.wants', script)
        self.assertIn('catalystlab-grow-root.service', script)
        self.assertIn('for TOOL in sfdisk partx resize2fs mountpoint', script)


class MachineRootConnectionTests(unittest.TestCase):
    def test_missing_machine_returns_error_without_starting(self):
        connection = deploy_image.MachineRootConnection(None)
        self.assertEqual(connection.connect(), 'No virtual machine to create image')
        self.assertFalse(connection.connected)

    def test_connect_starts_machine_and_close_releases_use(self):
        machine = Mock(name='builder')
        connection = deploy_image.MachineRootConnection(machine)
        self.assertIsNone(connection.connect())
        machine.begin_use.assert_called_once_with('Deploy image')
        machine.ensure_running.assert_called_once_with(print, None)
        self.assertTrue(connection.is_alive())
        connection.close()
        machine.end_use.assert_called_once_with('Deploy image')
        self.assertFalse(connection.connected)

    def test_connect_forwards_output_and_process_tracking(self):
        machine = Mock(name='builder')
        connection = deploy_image.MachineRootConnection(machine)
        output, processes = Mock(), []

        self.assertIsNone(connection.connect(output_handler=output, process_holder=processes))

        machine.ensure_running.assert_called_once_with(output, processes)

    def test_failed_machine_start_releases_use_and_returns_context(self):
        machine = Mock(name='builder')
        machine.name = 'builder'
        machine.ensure_running.side_effect = RuntimeError('offline')
        connection = deploy_image.MachineRootConnection(machine)
        self.assertEqual(connection.connect(), 'Failed to start virtual machine builder: offline')
        machine.end_use.assert_called_once_with('Deploy image')
        self.assertFalse(connection.connected)

    def test_run_encodes_script_and_forwards_to_machine_executor(self):
        connection = deploy_image.MachineRootConnection(Mock())
        executor = Mock()
        connection._executor = Mock(return_value=executor)
        output = Mock()
        holder = []
        self.assertTrue(connection.run('echo hello', output, holder))
        command = executor.run.call_args.args[0]
        self.assertIn('base64 -d', command)
        self.assertIn('sudo bash', command)
        self.assertEqual(executor.run.call_args.args[1:], (output, holder))

    def test_output_raises_last_command_line_on_failure(self):
        connection = deploy_image.MachineRootConnection(Mock())
        connection.run = Mock(side_effect=lambda script, callback: (callback('first'), callback('failure'), False)[-1])
        with self.assertRaisesRegex(RuntimeError, 'failure'):
            connection.output('false')

    def test_stream_file_forwards_progress_markers_and_command_output(self):
        connection = deploy_image.MachineRootConnection(Mock())
        connection.run = Mock()
        connection.run.side_effect = lambda script, handler, holder: (
            handler('@@catalystlab-deploy-progress 0.5'), handler('command output'), True
        )[-1]
        output, progress = [], []
        self.assertTrue(connection.stream_file('/tmp/file', 'cat', output.append, progress.append, []))
        self.assertEqual(progress, [0.5])
        self.assertEqual(output, ['command output'])


if __name__ == '__main__':
    unittest.main()
