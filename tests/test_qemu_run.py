"""QEMU command construction and host measurements without launching a VM."""
import ast
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import qemu_run as qemu
from catalystlab import repository


class QemuRunTests(unittest.TestCase):
    def test_vm_image_preparation_exposes_separate_workflow_steps(self):
        source = (ROOT / 'src/objects/qemu/qemu_run.py').read_text()
        tree = ast.parse(source)
        process = next(node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == 'QemuImagePreparation')
        setup = next(node for node in process.body
                     if isinstance(node, ast.FunctionDef) and node.name == 'setup_stages')
        namespace = {'_StepImage': lambda name, owner, operation: (name, operation)}
        exec(compile(ast.Module(body=[setup], type_ignores=[]), 'qemu_run.py', 'exec'), namespace)

        for kernel_package, expected in ((None, [('Prepare build VM', 'machine'),
                                                  ('Prepare stage image', 'cache'),
                                                  ('Create writable VM image', 'image')]),
                                         ('sys-kernel/gentoo-kernel', [('Prepare build VM', 'machine'),
                                                                       ('Prepare stage image', 'cache'),
                                                                       ('Create writable VM image', 'image'),
                                                                       ('Build and install custom kernel', 'kernel')])):
            with self.subTest(kernel_package=kernel_package):
                view = types.SimpleNamespace(support=types.SimpleNamespace(kernel_package=kernel_package), stages=[])
                namespace['setup_stages'](view)
                self.assertEqual(view.stages, expected)

    def test_kernel_stage_config_mount_does_not_include_merged_portage_mount(self):
        source = (ROOT / 'src/objects/qemu/qemu_run.py').read_text()
        tree = ast.parse(source)
        process = next(node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == '_StepImage')
        method = next(node for node in process.body
                      if isinstance(node, ast.FunctionDef) and node.name == '_emerge_kernel_package')
        mounts = [node for node in ast.walk(method) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name) and node.func.id == 'BindMount']
        stage_mount = next(node for node in mounts if any(
            keyword.arg == 'mount_path' and isinstance(keyword.value, ast.Constant)
        and keyword.value.value == '/var/tmp/catalystlab-stage-portage'
            for keyword in node.keywords))
        host_path = next(keyword.value for keyword in stage_mount.keywords if keyword.arg == 'host_path')

        self.assertIsInstance(host_path, ast.Call)
        self.assertEqual(ast.unparse(host_path), 'str(stage_config_directory)')

    def test_custom_kernel_uses_writable_root_overlay(self):
        source = (ROOT / 'src/objects/qemu/qemu_run.py').read_text()
        tree = ast.parse(source)
        process = next(node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == '_StepImage')
        method = next(node for node in process.body
                      if isinstance(node, ast.FunctionDef) and node.name == '_emerge_kernel_package')
        spawn = next(node for node in ast.walk(method) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and node.func.attr == 'spawn')

        self.assertTrue(next(keyword.value.value for keyword in spawn.keywords
                             if keyword.arg == 'writable_root_overlay'))

    def test_guest_image_cleanup_is_safe_when_mount_is_already_gone(self):
        script = qemu.stage_image_unmount_script('/temporary/image')

        self.assertIn('if mountpoint -q', script)
        self.assertIn('then umount', script)

    def test_custom_modules_are_extracted_inside_linux_image_from_archive(self):
        script = qemu.stage_image_add_modules_script('/temporary/image')

        self.assertIn('/temporary/image/custom-modules.tar', script)
        self.assertIn('tar -xf', script)
        self.assertIn('/temporary/image/rootfs/lib/modules', script)
        self.assertNotIn('cp -a', script)

    def test_kernel_build_lists_free_external_toolsets_after_project_toolset(self):
        from catalystlab.toolset import ToolsetEnv
        project_toolset = types.SimpleNamespace(busy_reason=None)
        alternative = types.SimpleNamespace(busy_reason=None, env=ToolsetEnv.EXTERNAL)
        project = types.SimpleNamespace(get_toolset=lambda: project_toolset)
        toolsets = types.SimpleNamespace(value=[project_toolset, alternative])

        with patch.object(repository.Repository, "Toolset", toolsets, create=True):
            self.assertEqual(qemu.kernel_build_toolsets(project), [project_toolset, alternative])

    def test_kernel_build_offers_free_external_toolsets_when_project_toolset_is_busy(self):
        from catalystlab.toolset import ToolsetEnv
        busy = types.SimpleNamespace(busy_reason="in use")
        alternative = types.SimpleNamespace(busy_reason=None, env=ToolsetEnv.EXTERNAL)
        unavailable = types.SimpleNamespace(busy_reason="in use", env=ToolsetEnv.EXTERNAL)
        project = types.SimpleNamespace(get_toolset=lambda: busy)
        toolsets = types.SimpleNamespace(value=[busy, alternative, unavailable])

        with patch.object(repository.Repository, "Toolset", toolsets, create=True):
            self.assertEqual(qemu.kernel_build_toolsets(project), [alternative])

    def test_custom_kernel_cache_resolves_tilde_in_library_location(self):
        settings = types.SimpleNamespace(cache_location='~/CatalystLab/Cache')
        build = types.SimpleNamespace(id='build-one', artifact_path='/missing/stage.tar.xz')
        snapshot = types.SimpleNamespace(filename='gentoo.sqfs', file_path=lambda: '/missing/gentoo.sqfs')
        with patch.object(repository.Repository, 'Settings', types.SimpleNamespace(value=settings), create=True):
            cache = qemu._kernel_output_cache_directory(build, 'arm64', 'sys-kernel/gentoo-kernel', snapshot, [])

        self.assertTrue(Path(cache).is_absolute())
        self.assertEqual(Path(cache).parts[-2], 'TestVMKernels')

    def test_custom_kernel_cache_is_separate_for_each_toolset(self):
        build = types.SimpleNamespace(id='build-one', artifact_path='/missing/stage.tar.xz')
        snapshot = types.SimpleNamespace(filename='gentoo.sqfs', file_path=lambda: '/missing/gentoo.sqfs')
        first = types.SimpleNamespace(uuid='first')
        second = types.SimpleNamespace(uuid='second')

        with patch.object(repository.Repository, 'Settings',
                          types.SimpleNamespace(value=types.SimpleNamespace(cache_location='~/CatalystLab/Cache')),
                          create=True):
            first_cache = qemu._kernel_output_cache_directory(
                build, 'arm64', 'sys-kernel/gentoo-kernel', snapshot, [], first)
            second_cache = qemu._kernel_output_cache_directory(
                build, 'arm64', 'sys-kernel/gentoo-kernel', snapshot, [], second)

        self.assertNotEqual(first_cache, second_cache)

    def test_stage_image_workspace_is_created_and_cleaned_in_library_temporary(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = types.SimpleNamespace(temporary_location=temporary)
            with patch.object(repository.Repository, 'Settings', types.SimpleNamespace(value=settings), create=True):
                directory = qemu.create_stage_image_directory()
                expected_root = Path(temporary).resolve() / 'TestVMImages' / 'stage-'
                self.assertTrue(directory.startswith(str(expected_root)))
                self.assertTrue(Path(directory).is_dir())
                qemu.cleanup_stage_image_directory(directory)
                self.assertFalse(Path(directory).exists())

    def test_cached_base_image_is_scoped_to_build_and_marks_only_complete_images_ready(self):
        with tempfile.TemporaryDirectory() as cache:
            settings = types.SimpleNamespace(cache_location=cache)
            support = qemu.QemuSupport(qemu.QemuBuildKind.STAGE, qemu._MACHINES['arm64'],
                                       kernel='Image', initramfs='initramfs')
            build = types.SimpleNamespace(id='build-one', artifact_path='/build/stage.tar.xz')
            with patch.object(repository.Repository, 'Settings', types.SimpleNamespace(value=settings), create=True):
                first = qemu.stage_image_cache_directory(build, support, 8 << 30)
                same = qemu.stage_image_cache_directory(build, support, 8 << 30)
                other_build = types.SimpleNamespace(id='build-two', artifact_path='/build/stage.tar.xz')
                other = qemu.stage_image_cache_directory(other_build, support, 8 << 30)
            self.assertEqual(first, same)
            self.assertNotEqual(first, other)
            Path(first).mkdir(parents=True)
            for name in ('root.img', 'Image', 'initramfs'):
                Path(first, name).touch()
            self.assertFalse(qemu.stage_base_image_ready(first, 'Image', 'initramfs'))
            Path(first, '.ready').touch()
            self.assertTrue(qemu.stage_base_image_ready(first, 'Image', 'initramfs'))

    def test_cached_base_image_copy_is_writable_and_keeps_cache_pristine(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, destination = Path(temporary, 'Cache'), Path(temporary, 'Temporary', 'run')
            source.mkdir()
            Path(source, 'root.img').write_bytes(b'base disk contents')
            Path(source, 'Image').write_bytes(b'kernel')
            Path(source, 'initramfs').write_bytes(b'initramfs')
            cloned = qemu.copy_stage_base_image(str(source), str(destination), 'Image', 'initramfs')
            self.assertIsInstance(cloned, bool)
            self.assertEqual(Path(destination, 'root.img').read_bytes(), b'base disk contents')
            self.assertEqual(Path(destination, 'Image').read_bytes(), b'kernel')
            self.assertEqual(Path(destination, 'initramfs').read_bytes(), b'initramfs')
            Path(destination, 'root.img').write_bytes(b'guest modifications')
            self.assertEqual(Path(source, 'root.img').read_bytes(), b'base disk contents')

    def test_base_image_can_be_prepared_without_a_stage_kernel(self):
        script = qemu.stage_image_script('/build/stage.tar.xz', '/temporary/run', 8 << 30, None, None)
        self.assertIn('mkfs.ext4', script)
        self.assertNotIn('cp "$MNT/boot/', script)
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'root.img').touch()
            Path(directory, '.ready').touch()
            self.assertTrue(qemu.stage_base_image_ready(directory, None, None))

    def test_cached_base_image_copy_stops_when_startup_is_canceled(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, destination = Path(temporary, 'Cache'), Path(temporary, 'Temporary', 'run')
            source.mkdir()
            Path(source, 'root.img').touch()
            canceled = threading.Event()
            canceled.set()
            with self.assertRaises(InterruptedError):
                qemu.copy_stage_base_image(str(source), str(destination), 'Image', None,
                                           process_holder=[], cancel_event=canceled)

    def make_run(self, kind=qemu.QemuBuildKind.STAGE, architecture='arm64', kernel='Image', initramfs='initramfs'):
        machine = qemu._MACHINES[architecture]
        support = qemu.QemuSupport(kind, machine, kernel=kernel, initramfs=initramfs,
                                   image_directory='/cache/TestVM/stage')
        build = types.SimpleNamespace(id='build', stage_name='stage4', artifact_path='/build/stage.tar.xz', path='/build')
        with patch.object(qemu, '_free_port', return_value=2222):
            run = qemu.QemuRun(types.SimpleNamespace(id='project'), build, support, memory_mib=8192, cpus=6)
        self.addCleanup(lambda: shutil.rmtree(run.directory, ignore_errors=True))
        self.addCleanup(lambda: qemu.QemuRun.running_runs.remove(run) if run in qemu.QemuRun.running_runs else None)
        return run

    def test_stage_command_uses_qcow_overlay_kernel_initramfs_and_rootwait(self):
        run = self.make_run()
        with patch.object(qemu, 'qemu_binary', side_effect=lambda name: '/qemu/' + name), \
             patch.object(qemu, '_firmware_path', return_value='/firmware/uefi.fd'), \
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

    def test_stage_command_uses_cached_base_and_boot_files_without_copying(self):
        run = self.make_run()
        run.support.base_image_path = '/library/Cache/TestVMImages/build/root.img'
        run.support.base_kernel_path = '/library/Cache/TestVMImages/build/Image'
        run.support.base_initramfs_path = '/library/Cache/TestVMImages/build/initramfs'
        with patch.object(qemu, 'qemu_binary', side_effect=lambda name: '/qemu/' + name), \
             patch.object(qemu, '_firmware_path', return_value='/firmware/uefi.fd'), \
             patch.object(qemu, 'accelerated', return_value=None):
            command = run.command()
        self.assertEqual(command[command.index('-kernel') + 1], run.support.base_kernel_path)
        self.assertEqual(command[command.index('-initrd') + 1], run.support.base_initramfs_path)

    def test_cached_qemu_stage_is_ready_without_temporary_root_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            image, kernel, initramfs = (Path(temporary, name) for name in ('root.img', 'Image', 'initramfs'))
            for path in (image, kernel, initramfs):
                path.touch()
            support = qemu.QemuSupport(qemu.QemuBuildKind.STAGE, qemu._MACHINES['arm64'], kernel='Image',
                                       initramfs='initramfs', backend='qemu', image_directory=temporary,
                                       base_image_path=str(image), base_kernel_path=str(kernel),
                                       base_initramfs_path=str(initramfs))
            self.assertTrue(qemu.stage_image_ready(support))

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
        with patch.object(qemu, 'qemu_binary', side_effect=lambda name: '/qemu/' + name), \
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
            run.support.image_directory = directory
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
