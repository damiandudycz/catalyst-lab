"""QEMU command construction and host measurements without launching a VM."""
import ast
import os
from pathlib import Path
import shutil
import sys
import subprocess
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
        namespace = {'_StepImage': lambda name, owner, operation: (name, operation),
                     'QemuBuildKind': qemu.QemuBuildKind}
        exec(compile(ast.Module(body=[setup], type_ignores=[]), 'qemu_run.py', 'exec'), namespace)

        for kernel_package, expected in ((None, [('Prepare Runner', 'machine'),
                                                  ('Prepare stage image', 'cache'),
                                                  ('Create writable VM image', 'image'),
                                                  ('Configure guest networking', 'network')]),
                                         ('sys-kernel/gentoo-kernel', [('Prepare Runner', 'machine'),
                                                                       ('Prepare stage image', 'cache'),
                                                                       ('Create writable VM image', 'image'),
                                                                       ('Build and install custom kernel', 'kernel'),
                                                                       ('Configure guest networking', 'network')])):
            with self.subTest(kernel_package=kernel_package):
                view = types.SimpleNamespace(support=types.SimpleNamespace(
                    kind=qemu.QemuBuildKind.STAGE, kernel_package=kernel_package, local_users=[], root_password=None,
                    host_resolv_conf=None, backend='qemu', network_enabled=True), stages=[])
                namespace['setup_stages'](view)
                self.assertEqual(view.stages, expected)

        support = types.SimpleNamespace(kind=qemu.QemuBuildKind.STAGE, kernel_package=None, local_users=[object()], root_password=None,
                                        host_resolv_conf=None, backend='qemu', network_enabled=True)
        view = types.SimpleNamespace(support=support, stages=[])
        namespace['setup_stages'](view)
        self.assertEqual(view.stages[-2:], [('Configure login accounts', 'accounts'),
                                            ('Configure guest networking', 'network')])

        support = types.SimpleNamespace(kind=qemu.QemuBuildKind.STAGE, kernel_package=None, local_users=[], root_password=None,
                                        host_resolv_conf=None, backend='apple', network_enabled=False)
        view = types.SimpleNamespace(support=support, stages=[])
        namespace['setup_stages'](view)
        self.assertEqual(view.stages[-1], ('Stop Runner', 'shutdown'))

        support = types.SimpleNamespace(kind=qemu.QemuBuildKind.STAGE, kernel_package=None, local_users=[], root_password=None,
                                        host_resolv_conf='nameserver 192.0.2.1\n', backend='qemu', network_enabled=False)
        view = types.SimpleNamespace(support=support, stages=[])
        namespace['setup_stages'](view)
        self.assertEqual(view.stages[-1], ('Configure host DNS', 'dns'))

    def test_host_resolver_script_writes_only_the_run_root(self):
        import base64
        content = 'search example.test\nnameserver 192.0.2.1\n'
        script = qemu.stage_image_configure_resolv_conf_script('/tmp/run', content)
        self.assertIn('rm -f "$ROOT/etc/resolv.conf"', script)
        self.assertIn('"$ROOT/etc/resolv.conf"', script)
        encoded = base64.b64encode(content.encode()).decode()
        self.assertIn(encoded, script)
        self.assertNotIn('/cache/', script)

    def test_stage_network_script_enables_systemd_networkd_and_dns_resolution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, 'rootfs')
            (root / 'usr/bin').mkdir(parents=True)
            (root / 'usr/bin/init').symlink_to('/usr/lib/systemd/systemd')
            units = root / 'usr/lib/systemd/system'
            units.mkdir(parents=True)
            (units / 'systemd-networkd.service').touch()
            (units / 'systemd-resolved.service').touch()
            script = qemu.stage_image_configure_network_script(str(temporary))
            result = subprocess.run(['sh', '-ec', script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            network = (root / 'etc/systemd/network/20-catalystlab.network').read_text()
            self.assertIn('Name=en* eth*', network)
            self.assertIn('DHCP=yes', network)
            self.assertTrue((root / 'etc/systemd/system/multi-user.target.wants/systemd-networkd.service').is_symlink())
            self.assertEqual(os.readlink(root / 'etc/resolv.conf'), '/run/systemd/resolve/stub-resolv.conf')

    def test_stage_network_script_falls_back_to_openrc_netifrc(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, 'rootfs')
            (root / 'usr/bin').mkdir(parents=True)
            (root / 'usr/bin/init').symlink_to('/sbin/openrc-init')
            (root / 'etc/init.d').mkdir(parents=True)
            (root / 'etc/init.d/net.lo').touch()
            (root / 'usr/sbin').mkdir(parents=True)
            (root / 'usr/sbin/dhcpcd').touch()
            (root / 'usr/sbin/dhcpcd').chmod(0o755)
            script = qemu.stage_image_configure_network_script(str(temporary))
            result = subprocess.run(['sh', '-ec', script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            config = (root / 'etc/conf.d/net').read_text()
            self.assertIn('config_enp0s1="dhcp"', config)
            self.assertIn('config_eth0="dhcp"', config)
            self.assertTrue((root / 'etc/runlevels/default/net.enp0s1').is_symlink())

    def test_qemu_can_disable_network_adapter(self):
        run = self.make_run()
        run.support.network_enabled = False
        with patch.object(qemu, 'qemu_binary', side_effect=lambda name: '/qemu/' + name), \
             patch.object(qemu, '_firmware_path', return_value='/firmware/uefi.fd'), \
             patch.object(qemu, 'accelerated', return_value=None):
            command = run.command()
        self.assertNotIn('-netdev', command)
        self.assertFalse(any(argument.startswith('virtio-net-') for argument in command))

    def test_vm_image_shutdown_step_stops_build_machine_before_finishing(self):
        machine = types.SimpleNamespace(name='Build machine', is_running=True, is_used=False)
        def stop(output_handler):
            machine.is_running = False
            return True
        machine.stop = Mock(side_effect=stop)
        step = object.__new__(qemu._StepImage)
        step.multistage_process = types.SimpleNamespace(project_directory=object())
        step.log = Mock()
        step._check_canceled = Mock()

        with patch('catalystlab.deploy_image.image_machine', return_value=machine):
            step._stop_build_machine()

        machine.stop.assert_called_once_with(output_handler=step.log)
        self.assertIn('Build VM stopped', [call.args[0] for call in step.log.call_args_list])

    def test_run_login_accounts_use_shared_deploy_configuration_in_run_image(self):
        from catalystlab.deploy_installation import DeployUser
        script = qemu.stage_image_configure_accounts_script(
            '/library/Temporary/TestVMImages/stage-run', 'root-secret',
            [DeployUser('tester', 'user-secret', groups=['users']),
             DeployUser('tester2', 'other-secret', groups=['wheel'])])

        self.assertIn("ROOT=/library/Temporary/TestVMImages/stage-run/rootfs", script)
        self.assertIn('mount --types proc /proc "$ROOT/proc"', script)
        self.assertIn('rm -f "$ROOT/etc/resolv.conf"\ncp -L /etc/resolv.conf "$ROOT/etc/resolv.conf"', script)
        self.assertIn('mount --rbind /dev "$ROOT/dev"', script)
        self.assertIn("in_root useradd", script)
        self.assertIn("tester:user-secret", script)
        self.assertIn("tester2:other-secret", script)
        self.assertIn("root:root-secret", script)
        self.assertIn("Verified login account tester", script)
        self.assertIn("Verified root password", script)
        self.assertNotIn('/Cache/', script)

    def test_qemu_copies_cached_base_before_configuring_run_accounts(self):
        support = qemu.QemuSupport(qemu.QemuBuildKind.STAGE, qemu._MACHINES['arm64'],
                                   kernel='Image', backend='qemu',
                                   local_users=[object()], image_directory='/temporary/run')
        process = types.SimpleNamespace(support=support)
        step = object.__new__(qemu._StepImage)
        step.multistage_process = process
        step.log = Mock()
        step.processes = []
        step._cancel_event = threading.Event()
        step._cache_directory = lambda: ('/cache/base', 8 << 30)

        with patch.object(qemu, 'copy_stage_base_image', return_value=True) as copy:
            step._prepare_writable_image()

        copy.assert_called_once_with('/cache/base', '/temporary/run', 'Image', None, [], step._cancel_event)
        self.assertIsNone(support.base_image_path)

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

    def test_kernel_build_lists_all_toolsets_for_disabled_choice_display(self):
        project_toolset = types.SimpleNamespace(busy_reason=None)
        alternative = types.SimpleNamespace(busy_reason=None)
        project = types.SimpleNamespace()
        toolsets = types.SimpleNamespace(value=[project_toolset, alternative])

        with patch.object(repository.Repository, "Toolset", toolsets, create=True):
            self.assertEqual(qemu.kernel_build_toolsets(project), [project_toolset, alternative])

    def test_kernel_build_keeps_busy_toolsets_visible_for_disabled_choice_display(self):
        busy = types.SimpleNamespace(busy_reason="in use")
        alternative = types.SimpleNamespace(busy_reason=None)
        unavailable = types.SimpleNamespace(busy_reason="in use")
        project = types.SimpleNamespace()
        toolsets = types.SimpleNamespace(value=[busy, alternative, unavailable])

        with patch.object(repository.Repository, "Toolset", toolsets, create=True):
            self.assertEqual(qemu.kernel_build_toolsets(project), [busy, alternative, unavailable])

    def test_custom_kernel_cache_resolves_tilde_in_library_location(self):
        settings = types.SimpleNamespace(cache_location='~/CatalystLab/Cache')
        toolset = types.SimpleNamespace(uuid='toolset-one')
        with patch.object(repository.Repository, 'Settings', types.SimpleNamespace(value=settings), create=True):
            cache = qemu._kernel_output_cache_directory(
                'arm64', 'sys-kernel/gentoo-kernel', toolset)

        self.assertTrue(Path(cache).is_absolute())
        self.assertEqual(Path(cache).parts[-2], 'TestVMKernels')

    def test_custom_kernel_cache_key_uses_architecture_package_and_toolset(self):
        settings = types.SimpleNamespace(cache_location='/cache')
        toolset = types.SimpleNamespace(uuid='toolset-one')
        other_toolset = types.SimpleNamespace(uuid='toolset-two')
        with patch.object(repository.Repository, 'Settings',
                          types.SimpleNamespace(value=settings),
                          create=True):
            same_inputs = qemu._kernel_output_cache_directory('arm64', 'sys-kernel/gentoo-kernel', toolset)
            repeated = qemu._kernel_output_cache_directory('arm64', 'sys-kernel/gentoo-kernel', toolset)
            other_toolset_cache = qemu._kernel_output_cache_directory(
                'arm64', 'sys-kernel/gentoo-kernel', other_toolset)
            other_package = qemu._kernel_output_cache_directory(
                'arm64', 'sys-kernel/gentoo-kernel-bin', toolset)
            other_architecture = qemu._kernel_output_cache_directory(
                'amd64', 'sys-kernel/gentoo-kernel', toolset)

        self.assertEqual(same_inputs, repeated)
        self.assertNotEqual(same_inputs, other_toolset_cache)
        self.assertNotEqual(same_inputs, other_package)
        self.assertNotEqual(same_inputs, other_architecture)

    def test_custom_kernel_cache_is_shared_across_stages_with_same_toolset(self):
        toolset = types.SimpleNamespace(uuid='shared-toolset')
        with patch.object(repository.Repository, 'Settings',
                          types.SimpleNamespace(value=types.SimpleNamespace(cache_location='/cache')),
                          create=True):
            stage_one_cache = qemu._kernel_output_cache_directory(
                'arm64', 'sys-kernel/gentoo-kernel', toolset)
            stage_two_cache = qemu._kernel_output_cache_directory(
                'arm64', 'sys-kernel/gentoo-kernel', toolset)
        self.assertEqual(stage_one_cache, stage_two_cache)

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
