"""Backend detection and native launch contract, without starting a guest."""
from pathlib import Path
import json
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(p) for p in (ROOT / 'src').rglob('*') if p.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import apple_virtualization as apple
from catalystlab import qemu_run as qemu

LIMITS = dict(min_cpus=1, max_cpus=8, min_memory=1 << 30, max_memory=32 << 30)


class BackendTests(unittest.TestCase):
    def setUp(self):
        apple._load_bridge.cache_clear()
        self.project = types.SimpleNamespace(get_architecture=lambda: types.SimpleNamespace(value='arm64'))
        self.build = types.SimpleNamespace(stage_name='stage3', path='/build', artifact_path='/build/stage.tar.xz')
        self.support = qemu.QemuSupport(qemu.QemuBuildKind.STAGE, qemu._MACHINES['arm64'], kernel='Image',
                                        initramfs='initramfs', image_directory='/cache/TestVM/hash')

    def detection(self, **kwargs):
        return apple.apple_support(self.project, self.build)

    def test_linux_never_probes_apple_helper(self):
        with patch.object(apple.sys, 'platform', 'linux'), patch.object(apple.subprocess, 'run') as run:
            self.assertFalse(self.detection().available)
            run.assert_not_called()

    def test_cross_architecture_never_probes_apple_helper(self):
        with patch.object(apple.sys, 'platform', 'darwin'), patch.object(apple.platform, 'machine', return_value='x86_64'), patch.object(apple.subprocess, 'run') as run:
            self.assertIn('architecture', self.detection().reason)
            run.assert_not_called()

    def test_native_detection_does_not_require_qemu_installation(self):
        with patch.object(apple.sys, 'platform', 'darwin'), patch.object(apple.platform, 'machine', return_value='arm64'), patch.object(apple, 'qemu_support', return_value=self.support) as support, patch.object(apple, 'apple_vm_library', return_value=sys.executable), patch.object(apple, '_probe_library', return_value=LIMITS):
            result = self.detection()
            self.assertTrue(result.available)
            self.assertEqual(result.support.backend, 'apple')
            self.assertEqual(result.support.machine.console, 'hvc0')
            self.assertEqual(self.support.machine.console, 'ttyAMA0')
            support.assert_called_once_with(self.project, self.build, check_runtime=False)

    def test_missing_native_helper_disables_backend(self):
        with patch.object(apple.sys, 'platform', 'darwin'), patch.object(apple.platform, 'machine', return_value='arm64'), patch.object(apple, 'qemu_support', return_value=self.support), patch.object(apple, 'apple_vm_library', return_value=None):
            self.assertIn('not installed', self.detection().reason)

    def test_failed_host_probe_disables_backend(self):
        import subprocess
        with patch.object(apple.sys, 'platform', 'darwin'), patch.object(apple.platform, 'machine', return_value='arm64'), patch.object(apple, 'qemu_support', return_value=self.support), patch.object(apple, 'apple_vm_library', return_value=sys.executable), patch.object(apple, '_probe_library', side_effect=RuntimeError('Virtualization unsupported')):
            self.assertIn('unsupported', self.detection().reason)

    def test_native_launch_passes_base_image_without_modifying_it(self):
        self.support.backend = 'apple'
        availability = apple.AppleSupport(self.support, sys.executable, LIMITS)
        run = apple.AppleVirtualizationRun(self.project, self.build, availability)
        self.addCleanup(lambda: __import__('shutil').rmtree(run.directory, ignore_errors=True))
        self.addCleanup(lambda: qemu.QemuRun.running_runs.remove(run) if run in qemu.QemuRun.running_runs else None)
        bridge = Mock()
        bridge.api.cl_vm_create.return_value = 123
        with patch.object(apple, 'apple_support', return_value=availability), patch.object(apple, '_load_bridge', return_value=bridge):
            run.start()
            config = json.loads(bridge.api.cl_vm_create.call_args.args[0])
            self.assertEqual(config['disk'], '/cache/TestVM/hash/root.img')
            self.assertEqual(config['kernel'], '/cache/TestVM/hash/Image')
            self.assertEqual(config['memory'], 4 << 30)
            self.assertIn(run, qemu.QemuRun.running_runs)
            self.assertTrue(run.is_running)
            run.power_off()
            bridge.api.cl_vm_command.assert_called_with(123, b'poweroff')
            run.place_display(456, 10, 20, 800, 600)
            bridge.api.cl_vm_place.assert_called_with(123, 456, 10, 20, 800, 600)
            run.detach_display()
            bridge.api.cl_vm_place.assert_called_with(123, None, 0, 0, 0, 0)
            run._handle_event('STATE:running')
            self.assertTrue(run.ready)
            run._handle_event('STATE:stopped')
            self.assertFalse(run.is_running)
            bridge.api.cl_vm_release.assert_called_once_with(123)

    def test_apple_run_directory_uses_library_temporary(self):
        from catalystlab import repository
        availability = apple.AppleSupport(self.support, '/bridge', LIMITS)
        with tempfile.TemporaryDirectory() as temporary:
            settings = types.SimpleNamespace(value=types.SimpleNamespace(temporary_location=temporary))
            with patch.object(repository.Repository, 'Settings', settings, create=True):
                run = apple.AppleVirtualizationRun(self.project, self.build, availability)
            try:
                self.assertTrue(Path(run.directory).resolve().is_relative_to((Path(temporary) / 'VMs').resolve()))
            finally:
                __import__('shutil').rmtree(run.directory, ignore_errors=True)

    def test_native_launch_rechecks_availability(self):
        availability = apple.AppleSupport(self.support, '/helper', LIMITS)
        run = apple.AppleVirtualizationRun(self.project, self.build, availability)
        with patch.object(apple, 'apple_support', return_value=apple.AppleSupport(reason='Helper removed')), patch.object(apple.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'Helper removed'):
                run.start()
            popen.assert_not_called()
            self.assertNotIn(run, qemu.QemuRun.running_runs)
            self.assertFalse(Path(run.directory).exists())

    def test_boot_metadata_is_available_without_qemu_but_runtime_requires_it(self):
        # A valid stage is eligible for native boot even if QEMU isn't installed.
        modules = {
            'catalystlab.project_build': types.SimpleNamespace(StageBuildStatus=types.SimpleNamespace(COMPLETED='done')),
            'catalystlab.deploy_live_iso': types.SimpleNamespace(is_live_iso_build=lambda build: False),
            'catalystlab.deploy_installation': types.SimpleNamespace(is_deployable=lambda project, build: True),
        }
        self.build.status = 'done'
        self.build.artifact = 'stage.tar.xz'
        with patch.dict(sys.modules, modules), patch.object(qemu.os.path, 'isfile', return_value=True), patch.object(qemu, '_stage_boot_files', return_value=(['Image'], ['initramfs'])), patch.object(qemu, 'qemu_binary', return_value=None):
            self.assertTrue(qemu.qemu_support(self.project, self.build, check_runtime=False).available)
            self.assertIn('not installed', qemu.qemu_support(self.project, self.build).reason)

    def test_stage_without_kernel_remains_available_for_custom_kernel_launch(self):
        modules = {
            'catalystlab.project_build': types.SimpleNamespace(StageBuildStatus=types.SimpleNamespace(COMPLETED='done')),
            'catalystlab.deploy_live_iso': types.SimpleNamespace(is_live_iso_build=lambda build: False),
            'catalystlab.deploy_installation': types.SimpleNamespace(is_deployable=lambda project, build: True),
        }
        self.build.status = 'done'
        self.build.artifact = 'stage.tar.xz'
        with patch.dict(sys.modules, modules), patch.object(qemu.os.path, 'isfile', return_value=True), \
             patch.object(qemu, '_stage_boot_files', return_value=([], [])), \
             patch.object(qemu, 'qemu_binary', return_value='/emulator'):
            support = qemu.qemu_support(self.project, self.build)
        self.assertTrue(support.available)
        self.assertEqual(support.kind, qemu.QemuBuildKind.STAGE)
        self.assertIsNone(support.kernel)
        self.assertIsNone(support.initramfs)

    def test_native_startup_error_is_retained_and_run_removed(self):
        availability = apple.AppleSupport(self.support, '/bridge', LIMITS)
        run = apple.AppleVirtualizationRun(self.project, self.build, availability)
        run._active = True
        run._handle = 123
        run._bridge = Mock()
        qemu.QemuRun.running_runs.append(run)
        run._handle_event('ERROR:Kernel cannot boot')
        run._handle_event('STATE:stopped')
        self.assertEqual(run.error, 'Kernel cannot boot')
        self.assertNotIn(run, qemu.QemuRun.running_runs)
        self.assertFalse(Path(run.directory).exists())
        run._bridge.api.cl_vm_release.assert_called_once_with(123)

    def test_silent_apple_guest_reports_missing_serial_output(self):
        availability = apple.AppleSupport(self.support, '/bridge', LIMITS)
        run = apple.AppleVirtualizationRun(self.project, self.build, availability)
        run._active = True
        with patch.object(apple.GLib, 'timeout_add_seconds', return_value=42) as schedule:
            run._handle_event('STATE:running')
        schedule.assert_called_once_with(30, run._report_missing_serial_output)
        self.assertTrue(run.ready)
        run._report_missing_serial_output()
        self.assertIn('No guest serial output after 30 seconds', run.output[-1])

    def test_native_launch_configuration_is_added_to_vm_log(self):
        run = apple.AppleVirtualizationRun(
            self.project, self.build, apple.AppleSupport(self.support))
        self.addCleanup(lambda: __import__('shutil').rmtree(run.directory, ignore_errors=True))
        run._active = True
        run._handle_event('LAUNCH: CPUs: 4')
        run._handle_event('LAUNCH: Kernel command line: root=/dev/vda rw rootwait console=hvc0')

        self.assertIn('CPUs: 4', run.output)
        self.assertIn('Kernel command line: root=/dev/vda rw rootwait console=hvc0', run.output)

    def test_stale_events_after_stop_do_not_reactivate_run(self):
        run = apple.AppleVirtualizationRun(self.project, self.build, apple.AppleSupport(self.support))
        self.addCleanup(lambda: __import__('shutil').rmtree(run.directory, ignore_errors=True))
        run._handle_event('STATE:running')
        self.assertFalse(run.ready)
        self.assertFalse(run.is_running)

    def test_qemu_stage_requires_image_tool(self):
        modules = {
            'catalystlab.project_build': types.SimpleNamespace(StageBuildStatus=types.SimpleNamespace(COMPLETED='done')),
            'catalystlab.deploy_live_iso': types.SimpleNamespace(is_live_iso_build=lambda build: False),
            'catalystlab.deploy_installation': types.SimpleNamespace(is_deployable=lambda project, build: True),
        }
        self.build.status = 'done'
        self.build.artifact = 'stage.tar.xz'
        with patch.dict(sys.modules, modules), patch.object(qemu.os.path, 'isfile', return_value=True), patch.object(qemu, '_stage_boot_files', return_value=(['Image'], [])), patch.object(qemu, 'qemu_binary', side_effect=lambda name: None if name == 'qemu-img' else '/emulator'):
            self.assertIn('qemu-img', qemu.qemu_support(self.project, self.build).reason)

    def test_qemu_iso_requires_firmware_but_native_metadata_does_not(self):
        modules = {
            'catalystlab.project_build': types.SimpleNamespace(StageBuildStatus=types.SimpleNamespace(COMPLETED='done')),
            'catalystlab.deploy_live_iso': types.SimpleNamespace(is_live_iso_build=lambda build: True),
            'catalystlab.deploy_installation': types.SimpleNamespace(is_deployable=lambda project, build: True),
        }
        self.build.status = 'done'
        self.build.artifact = 'live.iso'
        with patch.dict(sys.modules, modules), patch.object(qemu.os.path, 'isfile', return_value=True), patch.object(qemu, '_iso_kernel_architecture', return_value='arm64'), patch.object(qemu, 'qemu_binary', return_value='/emulator'), patch.object(qemu, '_firmware_path', return_value=None):
            self.assertIn('firmware', qemu.qemu_support(self.project, self.build).reason)
            self.assertTrue(qemu.qemu_support(self.project, self.build, check_runtime=False).available)

    def test_native_rejects_bios_only_iso(self):
        self.support.kind = qemu.QemuBuildKind.ISO
        with patch.object(apple.sys, 'platform', 'darwin'), patch.object(apple.platform, 'machine', return_value='arm64'), patch.object(apple, 'qemu_support', return_value=self.support), patch.object(apple.subprocess, 'run', return_value=types.SimpleNamespace(stdout='boot/grub/grub.cfg\nboot/Image\n')):
            self.assertIn('EFI-bootable', self.detection().reason)


if __name__ == '__main__':
    unittest.main()
