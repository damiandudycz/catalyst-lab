"""QEMU command construction and host measurements without launching a VM."""
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import qemu_run as qemu


class QemuRunTests(unittest.TestCase):
    def make_run(self, kind=qemu.QemuBuildKind.STAGE, architecture='arm64', kernel='Image', initramfs='initramfs'):
        machine = qemu._MACHINES[architecture]
        support = qemu.QemuSupport(kind, machine, kernel=kernel, initramfs=initramfs)
        build = types.SimpleNamespace(stage_name='stage4', artifact_path='/build/stage.tar.xz', path='/build')
        with patch.object(qemu, '_free_port', return_value=2222):
            run = qemu.QemuRun(types.SimpleNamespace(id='project'), build, support, memory_mib=8192, cpus=6)
        self.addCleanup(lambda: shutil.rmtree(run.directory, ignore_errors=True))
        self.addCleanup(lambda: qemu.QemuRun.running_runs.remove(run) if run in qemu.QemuRun.running_runs else None)
        return run

    def test_stage_command_uses_qcow_overlay_kernel_initramfs_and_rootwait(self):
        run = self.make_run()
        with patch.object(qemu, 'qemu_binary', side_effect=lambda name: '/qemu/' + name), \
             patch.object(qemu, '_firmware_path', return_value='/firmware/uefi.fd'), \
             patch.object(qemu, 'stage_image_directory', return_value='/cache/TestVM/stage'), \
             patch.object(qemu, 'accelerated', return_value=None):
            command = run.command()
        self.assertIn('-m', command)
        self.assertEqual(command[command.index('-m')+1], '8192')
        self.assertEqual(command[command.index('-smp')+1], '6')
        self.assertIn('-accel', command)
        self.assertEqual(command[command.index('-accel')+1], 'tcg')
        self.assertIn(f'file={run.overlay},format=qcow2,if=virtio', command)
        self.assertEqual(command[command.index('-kernel')+1], '/cache/TestVM/stage/Image')
        self.assertEqual(command[command.index('-initrd')+1], '/cache/TestVM/stage/initramfs')
        self.assertEqual(command[command.index('-append')+1], 'root=/dev/vda rw rootwait console=tty0 console=ttyAMA0')
        self.assertIn('qemu-xhci', command)
        self.assertIn(f'unix:{run.vnc_socket}', command)
        self.assertIn(f'unix:{run.qmp_socket},server=on,wait=off', command)

    def test_iso_command_attaches_read_only_cd_and_omits_stage_boot_flags(self):
        run = self.make_run(kind=qemu.QemuBuildKind.ISO, architecture='amd64', kernel=None, initramfs=None)
        with patch.object(qemu, 'qemu_binary', side_effect=lambda name: '/qemu/' + name), \
             patch.object(qemu, '_firmware_path', return_value='/firmware/OVMF.fd'), \
             patch.object(qemu, 'accelerated', return_value='hvf'):
            command = run.command()
        self.assertIn('-accel', command)
        self.assertEqual(command[command.index('-accel')+1], 'hvf')
        self.assertIn('file=/build/stage.tar.xz,format=raw,readonly=on,media=cdrom,if=none,id=cd', command)
        self.assertIn('virtio-scsi-pci', command)
        self.assertIn('scsi-cd,drive=cd', command)
        self.assertNotIn('-kernel', command)
        self.assertNotIn('-initrd', command)

    def test_s390x_uses_ccw_disk_device_and_omits_usb_controller(self):
        run = self.make_run(architecture='s390x')
        with patch.object(qemu, 'qemu_binary', side_effect=lambda name: '/qemu/' + name), \
             patch.object(qemu, 'stage_image_directory', return_value='/cache/TestVM/stage'), \
             patch.object(qemu, 'accelerated', return_value=None):
            command = run.command()
        self.assertIn(f'file={run.overlay},format=qcow2,if=none,id=disk', command)
        self.assertIn('virtio-blk-ccw,drive=disk', command)
        self.assertNotIn('qemu-xhci', command)
        self.assertIn('root=/dev/vda rw rootwait console=tty0 console=ttysclp0', command)

    def test_command_fails_clearly_when_qemu_binary_is_missing(self):
        run = self.make_run()
        with patch.object(qemu, 'qemu_binary', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'qemu-system-aarch64 is not installed'):
                run.command()

    def test_stage_start_creates_disposable_overlay_before_launch(self):
        run = self.make_run()
        process = Mock()
        process.poll.return_value = None
        with patch.object(qemu, 'stage_image_directory', return_value='/cache/TestVM/stage'), \
             patch.object(qemu, 'qemu_binary', side_effect=lambda name: '/qemu/' + name), \
             patch.object(run, 'command', return_value=['/qemu/qemu-system-aarch64']), \
             patch.object(qemu.subprocess, 'run') as image_create, \
             patch.object(qemu.subprocess, 'Popen', return_value=process), \
             patch.object(qemu.threading, 'Thread') as thread:
            run._start_locked()
        image_create.assert_called_once_with([
            '/qemu/qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'raw',
            '-b', '/cache/TestVM/stage/root.img', run.overlay,
        ], check=True)
        self.assertIs(run.process, process)
        self.assertIn(run, qemu.QemuRun.running_runs)
        self.assertEqual(thread.call_count, 2)

    def test_iso_start_does_not_create_stage_overlay(self):
        run = self.make_run(kind=qemu.QemuBuildKind.ISO, architecture='amd64', kernel=None, initramfs=None)
        process = Mock()
        process.poll.return_value = None
        with patch.object(run, 'command', return_value=['qemu']), patch.object(qemu.subprocess, 'run') as image_create, \
             patch.object(qemu.subprocess, 'Popen', return_value=process), patch.object(qemu.threading, 'Thread'):
            run._start_locked()
        image_create.assert_not_called()
        self.assertIn(run, qemu.QemuRun.running_runs)

    def test_read_usage_normalizes_cpu_and_reports_resident_bytes(self):
        run = self.make_run()
        run.process = types.SimpleNamespace(pid=123, poll=lambda: None)
        result = types.SimpleNamespace(stdout='250.0 1024\n')
        with patch.object(qemu.os.path, 'isfile', return_value=True), patch.object(qemu.os, 'access', return_value=True), \
             patch.object(qemu.subprocess, 'run', return_value=result) as ps:
            self.assertEqual(run.read_usage(), (0.4166666666666667, 1024 * 1024))
        self.assertIn('LC_ALL', ps.call_args.kwargs['env'])

    def test_read_usage_returns_none_for_stopped_or_unreadable_process(self):
        run = self.make_run()
        run.process = types.SimpleNamespace(pid=123, poll=lambda: 0)
        self.assertIsNone(run.read_usage())
        run.process.poll = lambda: None
        with patch.object(qemu.os.path, 'isfile', return_value=False), patch.object(qemu.shutil, 'which', return_value=None):
            self.assertIsNone(run.read_usage())

    def test_read_disk_usage_reports_overlay_allocation_and_virtual_capacity(self):
        run = self.make_run()
        run.process = types.SimpleNamespace(poll=lambda: None)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory, 'root.img')
            base.write_bytes(b'x' * 4096)
            overlay = Path(run.overlay)
            overlay.write_bytes(b'y' * 128)
            with patch.object(qemu, 'stage_image_directory', return_value=directory):
                usage = run.read_disk_usage()
        self.assertEqual(usage[1], 4096)
        self.assertGreater(usage[0], 0)

    def test_running_for_build_matches_project_and_artifact(self):
        run = self.make_run()
        run.process = types.SimpleNamespace(poll=lambda: None)
        qemu.QemuRun.running_runs.append(run)
        same = types.SimpleNamespace(id='project')
        different = types.SimpleNamespace(id='other')
        self.assertIs(qemu.QemuRun.running_for_build(same, run.build), run)
        self.assertIsNone(qemu.QemuRun.running_for_build(different, run.build))
        self.assertIsNone(qemu.QemuRun.running_for_build(same, types.SimpleNamespace(artifact_path='/different')))


if __name__ == '__main__':
    unittest.main()
