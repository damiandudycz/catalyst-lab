"""Rootless executor contracts and safe local filesystem operations."""
from pathlib import Path
import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import rootless, project_build_rootless


class FakeProcess:
    def __init__(self, output=(), code=0, canceled=False):
        self.stdout = output
        self.code = code
        self.canceled = canceled
        self.wait_calls = []

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.code


class FakeExecutor(rootless.Executor):
    def __init__(self, process=None):
        self.process = process or FakeProcess()
        self.started = []

    def cache_directory(self):
        return '/library/Cache'

    def temporary_directory(self):
        return '/library/Temporary'

    def unsupported_reason(self):
        return None

    def toolset_unsupported_reason(self, output_handler=None):
        return None

    def start(self, script, namespace=False):
        self.started.append((script, namespace))
        return self.process


class FilesystemOperationTests(unittest.TestCase):
    def test_local_filesystem_lists_inspects_writes_symlinks_and_removes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root/'folder'/'file.txt'
            link = root/'link'
            rootless._filesystem_operations({
                'mkdir': [str(target.parent)],
                'write': [(str(target), 'content')],
                'symlink': [(str(target), str(link))],
            })
            result = rootless._filesystem_operations({
                'info': [str(target), str(link), str(root/'missing')],
                'list': [str(target.parent), str(root/'missing')],
            })
            self.assertEqual(target.read_text(), 'content')
            self.assertEqual(result['info'][str(target)]['type'], 'file')
            self.assertEqual(result['info'][str(link)], {'type': 'link', 'target': str(target)})
            self.assertEqual(result['info'][str(root/'missing')], {'type': None})
            self.assertEqual(result['list'][str(target.parent)], [['file.txt', 'file']])
            self.assertIsNone(result['list'][str(root/'missing')])
            rootless._filesystem_operations({'remove': [str(link), str(target)]})
            self.assertFalse(link.exists())
            self.assertFalse(target.exists())

    def test_filesystem_remove_does_not_follow_symlink_to_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root/'target'
            target.mkdir()
            file = target/'keep'
            file.write_text('safe')
            link = root/'link'
            link.symlink_to(target, target_is_directory=True)
            rootless._filesystem_operations({'remove': [str(link)]})
            self.assertFalse(link.is_symlink())
            self.assertEqual(file.read_text(), 'safe')


class ExecutorTests(unittest.TestCase):
    def test_local_namespace_executor_passes_script_file_to_launcher_and_cleans_it(self):
        process = Mock()
        process.stdout = None
        process.wait.return_value = 0
        script = "echo " + ("payload" * 1000)

        with patch('catalystlab.rootless.subprocess.Popen', return_value=process) as popen:
            running = rootless.LocalExecutor().start(script, namespace=True)

        arguments = popen.call_args.args[0]
        script_path = arguments[-1]
        self.assertTrue(os.path.isfile(script_path))
        self.assertFalse(os.path.basename(script_path).startswith('.'))
        self.assertEqual(Path(script_path).read_text(), script)
        self.assertLess(len(script_path), 256)
        running.wait()
        self.assertFalse(os.path.exists(script_path))

    def test_path_containment_normalizes_parent_components_and_safely_rejects_other_roots(self):
        self.assertTrue(rootless.path_is_within('/library/Cache/Extracted', '/library/Cache'))
        self.assertTrue(rootless.path_is_within('/library/Cache', '/library/Cache'))
        self.assertFalse(rootless.path_is_within('/library/Cache/../outside', '/library/Cache'))
        with patch('catalystlab.rootless.os.path.commonpath', side_effect=ValueError):
            self.assertFalse(rootless.path_is_within('/other-drive/file', '/library/Cache'))

    def test_path_containment_resolves_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, 'library')
            outside = Path(temporary, 'outside')
            root.mkdir()
            outside.mkdir()
            (root / 'link').symlink_to(outside, target_is_directory=True)
            self.assertFalse(rootless.path_is_within(root / 'link' / 'file', root))

    def test_interrupted_build_cache_changes_are_saved_before_sessions_are_removed(self):
        executor = FakeExecutor()
        with patch.object(project_build_rootless, 'sessions_directory', return_value='/library/Temporary/Sessions'):
            saved = project_build_rootless.save_interrupted_caches(executor, lambda _line: None)

        script, namespace = executor.started[0]
        self.assertTrue(saved)
        self.assertTrue(namespace)
        self.assertIn('/library/Temporary/Sessions/build-*/caches/*/saved', script)
        self.assertIn('save_cache_changes "$directory/upper" "$saved"', script)
        self.assertIn('rm -rf --one-file-system /library/Temporary/Sessions/build-*', script)

    def test_catalyst_snapshot_uses_the_normal_command_without_key_refresh_overrides(self):
        self.assertEqual(project_build_rootless.catalyst_snapshot_command(), 'catalyst -s stable')

    def test_extracted_squashfs_stages_next_to_cache_for_same_filesystem_publish(self):
        executor = FakeExecutor()
        path = rootless.extracted_squashfs(
            '/library/Toolsets/toolset.sqfs', 'toolsets', lambda _line: None,
            check_path='usr', executor=executor)

        self.assertEqual(path, '/library/Cache/Extracted/Toolsets/toolset.sqfs.d')
        script, namespace = executor.started[0]
        self.assertTrue(namespace)
        self.assertIn('TEMP_PATH=/library/Cache/Extracted/Toolsets/.toolset.sqfs.', script)
        self.assertIn('mv -T -- "$TEMP_PATH"', script)
        self.assertIn('set -euo pipefail', script)
        self.assertNotIn('/library/Temporary/Work/toolset.sqfs.', script)

    def test_machine_extracted_squashfs_stays_on_case_sensitive_workspace_filesystem(self):
        class MachineFakeExecutor(FakeExecutor):
            is_machine = True

        executor = MachineFakeExecutor()
        path = rootless.extracted_squashfs(
            '/library/Toolsets/toolset.sqfs', 'toolsets', lambda _line: None,
            check_path='usr', executor=executor)

        self.assertEqual(path, '/library/Temporary/Work/Extracted/Toolsets/toolset.sqfs.d')
        script, _ = executor.started[0]
        self.assertIn('TEMP_PATH=/library/Temporary/Work/Extracted/Toolsets/.toolset.sqfs.', script)
        self.assertIn('mv -T -- "$TEMP_PATH" /library/Temporary/Work/Extracted/Toolsets/toolset.sqfs.d', script)

    def test_machine_squashfs_is_mounted_read_only_without_extraction(self):
        class MachineFakeExecutor(FakeExecutor):
            is_machine = True

            def filesystem(self, request):
                self.filesystem_requests = getattr(self, 'filesystem_requests', []) + [request]
                return {}

        executor = MachineFakeExecutor()
        path = rootless.mount_squashfs_read_only(
            '/library/Toolsets/toolset.sqfs', lambda _line: None, executor=executor)

        self.assertTrue(path.startswith('/library/Temporary/Work/toolsets/mount_'))
        self.assertEqual(executor.filesystem_requests, [{'mkdir': [path]}])
        script, namespace = executor.started[0]
        self.assertFalse(namespace)
        self.assertIn('sudo mount -t squashfs -o loop,ro /library/Toolsets/toolset.sqfs', script)
        self.assertIn(f'mountpoint -q {path}', script)
        self.assertNotIn('unsquashfs', script)

    def test_build_machine_toolset_uses_mounted_lower_layer(self):
        class MachineFakeExecutor(FakeExecutor):
            is_machine = True

        executor = MachineFakeExecutor()
        with patch.object(rootless, 'MOUNT_SQFS_DIRECTLY', True), \
                patch.object(rootless, 'mount_squashfs_read_only', return_value='/workspace/toolsets/mount_abc') as mount, \
                patch.object(rootless, 'extracted_squashfs') as extract:
            path, mounted, temporary_copy = rootless.prepare_toolset_squashfs(
                '/library/Toolsets/toolset.sqfs', lambda _line: None, executor=executor)

        self.assertEqual(path, '/workspace/toolsets/mount_abc')
        self.assertTrue(mounted)
        self.assertFalse(temporary_copy)
        mount.assert_called_once_with('/library/Toolsets/toolset.sqfs', unittest.mock.ANY, None,
                                      executor, kind='toolsets')
        extract.assert_not_called()

    def test_default_toolset_mode_copies_reusable_extraction_to_temporary(self):
        executor = FakeExecutor()
        with patch.object(rootless, 'extracted_squashfs', return_value='/library/Cache/Extracted/Toolsets/toolset.sqfs.d') as extract, \
                patch.object(rootless, 'create_work_directory', return_value='/library/Temporary/Work/toolsets/copy_test'), \
                patch.object(executor, 'run_in_namespace', return_value=True) as run, \
                patch.object(rootless, 'mount_squashfs_read_only') as mount:
            path, mounted, temporary_copy = rootless.prepare_toolset_squashfs(
                '/library/Toolsets/toolset.sqfs', lambda _line: None, executor=executor)

        self.assertEqual(path, '/library/Temporary/Work/toolsets/copy_test')
        self.assertFalse(mounted)
        self.assertTrue(temporary_copy)
        extract.assert_called_once_with('/library/Toolsets/toolset.sqfs', 'toolsets', unittest.mock.ANY, None,
                                        check_path='bin/bash', executor=executor)
        run.assert_called_once()
        mount.assert_not_called()

    def test_extracted_toolset_mode_copies_reusable_extraction_to_temporary(self):
        executor = FakeExecutor()
        with patch.object(rootless, 'extracted_squashfs', return_value='/library/Cache/Extracted/toolset.d') as extract, \
                patch.object(rootless, 'create_work_directory', return_value='/library/Temporary/Work/toolsets/copy_test'), \
                patch.object(executor, 'run_in_namespace', return_value=True) as run:
            path, mounted, temporary_copy = rootless.prepare_toolset_squashfs(
                '/library/Toolsets/toolset.sqfs', lambda _line: None, executor=executor)

        self.assertEqual(path, '/library/Temporary/Work/toolsets/copy_test')
        self.assertFalse(mounted)
        self.assertTrue(temporary_copy)
        extract.assert_called_once_with('/library/Toolsets/toolset.sqfs', 'toolsets', unittest.mock.ANY, None,
                                        check_path='bin/bash', executor=executor)
        run.assert_called_once()
        self.assertIn('cp -a -- /library/Cache/Extracted/toolset.d/.', run.call_args.args[0])

    def test_build_session_prepares_bind_targets_in_overlay_upper_before_mount(self):
        executor = FakeExecutor()
        with patch.object(project_build_rootless, 'sessions_directory', return_value='/workspace/Temporary/Sessions'):
            script = project_build_rootless.stage_build_session_script(
                executor, '/workspace/Toolsets/mount',
                [('/library/Snapshots', '/var/tmp/catalyst/snapshots')], 'catalyst -s stable')

        prepare = script.index('prepare_bind_target /var/tmp/catalyst/snapshots || exit 1')
        overlay = script.index('mount -t overlay overlay -o lowerdir=/workspace/Toolsets/mount')
        bind = script.index('bind /library/Snapshots /var/tmp/catalyst/snapshots')
        self.assertLess(prepare, overlay)
        self.assertLess(overlay, bind)
        self.assertNotIn('PATCH_TOOLSET_FILE', script)
        self.assertIn('mode=$(stat -c %a "$TOOLSET$current")', script)
        self.assertIn('chmod "$mode" "$UPPER$current"', script)

    def test_build_session_makes_synced_cache_writable_to_portage_and_checks_access(self):
        executor = FakeExecutor()
        with patch.object(project_build_rootless, 'sessions_directory', return_value='/workspace/Temporary/Sessions'):
            script = project_build_rootless.stage_build_session_script(
                executor, '/workspace/Toolsets/mount',
                [('/library/Snapshots', '/var/db/repos/gentoo'),
                 ('/vm/Cache/Distfiles', '/var/cache/distfiles')], 'catalyst --build',
                synced_caches=[('/library/Cache/Distfiles', '/var/cache/distfiles')])

        resolve_ids = script.index('PORTAGE_UID=$(awk')
        cache_chown = script.index('chown "$PORTAGE_UID:$PORTAGE_GID" "$SESSION/caches/0/upper"')
        cache_mount = script.index('mount -t overlay overlay -o lowerdir=/library/Cache/Distfiles')
        access_check = script.index('Checking Gentoo snapshot and distfiles access as portage')
        command = script.index('chroot "$ROOT" /usr/bin/env -i')
        self.assertLess(resolve_ids, cache_chown)
        self.assertLess(cache_chown, cache_mount)
        self.assertLess(cache_mount, access_check)
        self.assertLess(access_check, command)
        self.assertIn('open("/var/db/repos/gentoo/profiles/thirdpartymirrors", "rb")', script)
        self.assertIn('/usr/bin/python3 - <<\'PY_PORTAGE_ACCESS\'', script)
        self.assertIn('os.setuid(account.pw_uid)', script)
        self.assertIn('catalystlab-portage-access-test-', script)
        self.assertNotIn('bind /vm/Cache/Distfiles /var/cache/distfiles', script)

    def test_snapshot_generation_does_not_run_portage_access_preflight(self):
        executor = FakeExecutor()
        with patch.object(project_build_rootless, 'sessions_directory', return_value='/workspace/Temporary/Sessions'):
            script = project_build_rootless.stage_build_session_script(
                executor, '/workspace/Toolsets/mount',
                [('/library/Snapshots', '/var/tmp/catalyst/snapshots')], 'catalyst -s stable')

        self.assertNotIn('Checking Gentoo snapshot and distfiles access as portage', script)
        self.assertNotIn('PORTAGE_UID=$(awk', script)

    def test_build_session_overrides_toolset_resolver_symlink_in_writable_overlay(self):
        executor = FakeExecutor()
        with patch.object(project_build_rootless, 'sessions_directory', return_value='/workspace/Temporary/Sessions'):
            script = project_build_rootless.stage_build_session_script(
                executor, '/workspace/Toolsets/mount', [], 'true')

        resolver_overlay = script.index('cat /etc/resolv.conf > "$UPPER/etc/resolv.conf"')
        overlay_mount = script.index('mount -t overlay overlay -o lowerdir=/workspace/Toolsets/mount')
        chroot = script.index('chroot "$ROOT"')
        self.assertLess(resolver_overlay, overlay_mount)
        self.assertLess(overlay_mount, chroot)
        self.assertNotIn('cp -L /etc/resolv.conf', script)

    def test_machine_executor_passes_scripts_by_shared_temporary_file(self):
        class Machine:
            instance_name = 'catalystlab-test'
            lima_home = '/library/Machines'

            def __init__(self):
                self.ended = 0

            def begin_use(self):
                pass

            def ensure_running(self):
                pass

            def end_use(self):
                self.ended += 1

        class Process:
            stdout = []

        machine = Machine()
        script = 'echo test\n' * 2000
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(rootless.Repository, 'Settings', SimpleNamespace(
                    value=SimpleNamespace(temporary_location=temporary)), create=True), \
                patch('catalystlab.lima.limactl_path', return_value='/usr/bin/limactl'), \
                patch('catalystlab.lima.lima_environment', return_value={}), \
                patch.object(rootless.subprocess, 'Popen', return_value=Process()) as popen:
            process = rootless.MachineExecutor(machine).start(script, namespace=True)

            command = popen.call_args.args[0]
            script_path = next((Path(temporary) / 'MachineCommands').glob('*.sh'))
            self.assertTrue(str(script_path).startswith(os.path.join(temporary, 'MachineCommands')))
            self.assertEqual(script_path.read_text(), script)
            self.assertIn('cat "$1" > "$INNER"', command[-1])
            self.assertIn(str(script_path), command[-1])
            self.assertNotIn('base64', ' '.join(command))

            process._on_exit()
            self.assertFalse(script_path.exists())
            self.assertEqual(machine.ended, 1)

    def test_build_machine_snapshot_uses_read_only_mount_instead_of_extraction(self):
        class MachineFakeExecutor(FakeExecutor):
            is_machine = True

        executor = MachineFakeExecutor()
        with patch.object(rootless, 'mount_squashfs_read_only', return_value='/workspace/snapshots/mount_abc') as mount, \
                patch.object(rootless, 'extracted_squashfs') as extract:
            path, mounted = rootless.prepare_snapshot_squashfs(
                '/library/Snapshots/gentoo-test.sqfs', lambda _line: None, executor=executor)

        self.assertEqual(path, '/workspace/snapshots/mount_abc')
        self.assertTrue(mounted)
        mount.assert_called_once_with('/library/Snapshots/gentoo-test.sqfs', unittest.mock.ANY, None,
                                      executor, kind='snapshots')
        extract.assert_not_called()

    def test_local_snapshot_keeps_extracted_fallback(self):
        executor = FakeExecutor()
        with patch.object(rootless, 'extracted_squashfs', return_value='/library/Cache/Extracted/Snapshots/gentoo.sqfs.d') as extract, \
                patch.object(rootless, 'mount_squashfs_read_only') as mount:
            path, mounted = rootless.prepare_snapshot_squashfs(
                '/library/Snapshots/gentoo.sqfs', lambda _line: None, executor=executor)

        self.assertEqual(path, '/library/Cache/Extracted/Snapshots/gentoo.sqfs.d')
        self.assertFalse(mounted)
        extract.assert_called_once_with('/library/Snapshots/gentoo.sqfs', 'snapshots', unittest.mock.ANY, None,
                                        check_path='profiles', executor=executor)
        mount.assert_not_called()

    def test_machine_squashfs_mount_failure_removes_mountpoint(self):
        class MachineFakeExecutor(FakeExecutor):
            is_machine = True

            def filesystem(self, request):
                self.filesystem_requests = getattr(self, 'filesystem_requests', []) + [request]
                return {}

        executor = MachineFakeExecutor(FakeProcess(code=1))
        with self.assertRaisesRegex(RuntimeError, 'Failed to mount toolset.sqfs read-only'):
            rootless.mount_squashfs_read_only(
                '/library/Toolsets/toolset.sqfs', lambda _line: None, executor=executor)

        path = executor.filesystem_requests[0]['mkdir'][0]
        cleanup_script, namespace = executor.started[-1]
        self.assertFalse(namespace)
        self.assertIn(f'rmdir -- {path}', cleanup_script)

    def test_machine_squashfs_unmounts_and_removes_mountpoint(self):
        class MachineFakeExecutor(FakeExecutor):
            is_machine = True

        executor = MachineFakeExecutor()
        rootless.unmount_read_only_mount('/library/Temporary/Work/toolsets/mount_abc',
                                         lambda _line: None, executor=executor)

        script, namespace = executor.started[0]
        self.assertFalse(namespace)
        self.assertIn('sudo umount -- /library/Temporary/Work/toolsets/mount_abc', script)
        self.assertIn('rmdir -- /library/Temporary/Work/toolsets/mount_abc', script)

    def test_machine_squashfs_unmount_failure_reports_mounts_and_holders(self):
        class MachineFakeExecutor(FakeExecutor):
            is_machine = True

        executor = MachineFakeExecutor(FakeProcess(output=('umount: target is busy',), code=1))
        output = []
        with self.assertRaisesRegex(RuntimeError, 'Failed to unmount read-only image'):
            rootless.unmount_read_only_mount('/library/Temporary/Work/toolsets/mount_abc',
                                             output.append, executor=executor)

        script, namespace = executor.started[0]
        self.assertFalse(namespace)
        self.assertIn('umount_error=$(sudo umount --', script)
        self.assertIn('findmnt --submounts --target', script)
        self.assertIn('sudo fuser -vm --', script)
        self.assertIn('Processes using the mount:', script)

    def test_machine_toolset_extraction_is_cached_in_ext4_and_mounted_read_only(self):
        class MachineFakeExecutor(FakeExecutor):
            is_machine = True

            def __init__(self, cache, temporary):
                super().__init__()
                self.cache = cache
                self.temporary = temporary
                self.machine = SimpleNamespace(instance_name='catalystlab-test')

            def cache_directory(self):
                return self.cache

            def temporary_directory(self):
                return self.temporary

            def filesystem(self, request):
                return rootless._filesystem_operations(request)

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, 'toolset.sqfs')
            source.write_bytes(b'squashfs')
            executor = MachineFakeExecutor(str(Path(temporary, 'Cache')), str(Path(temporary, 'Temporary')))

            first_path = rootless.mount_cached_machine_toolset(str(source), lambda _line: None, executor=executor)
            scripts = [script for script, _namespace in executor.started]

            self.assertIn('/Cache/Extracted/Toolsets/catalystlab-test-', scripts[0])
            self.assertIn('mkfs.ext4 -F -m 0', scripts[0])
            self.assertIn('mount -o loop,rw', scripts[0])
            self.assertIn('unsquashfs -n', scripts[1])
            self.assertIn('mount -o remount,ro', scripts[2])
            self.assertTrue(first_path.endswith('/toolset.sqfs.d'))

            executor.started.clear()
            second_path = rootless.mount_cached_machine_toolset(str(source), lambda _line: None, executor=executor)
            scripts = [script for script, _namespace in executor.started]

            self.assertEqual(second_path.rsplit('/', 1)[-1], first_path.rsplit('/', 1)[-1])
            self.assertIn('Using cached extracted', scripts[1])
            self.assertIn('cat ', scripts[1])
            self.assertIn('mount -o remount,ro', scripts[2])

            image = rootless.machine_toolset_cache_image(str(source), executor)
            rootless.remove_extracted_squashfs(str(source), 'toolsets', executor)
            self.assertFalse(os.path.exists(image))

    def test_owned_path_requires_directory_boundary(self):
        executor = FakeExecutor()
        self.assertTrue(executor.owns_path('/library/Cache'))
        self.assertTrue(executor.owns_path('/library/Cache/Extracted/item'))
        self.assertTrue(executor.owns_path('/library/Temporary/Work'))
        self.assertFalse(executor.owns_path('/library/Cache-old/item'))
        self.assertFalse(executor.owns_path('/outside/file'))

    def test_run_streams_trimmed_output_and_records_process(self):
        process = FakeProcess(['first\n', 'second\r\n'], code=0)
        executor = FakeExecutor(process)
        output, holder = [], []
        self.assertTrue(executor.run('do work', output.append, holder))
        self.assertEqual(output, ['first', 'second\r'])
        self.assertEqual(holder, [process])
        self.assertEqual(executor.started, [('do work', False)])
        self.assertEqual(process.wait_calls, [None])

    def test_run_in_namespace_sets_flag_and_failure_returns_false(self):
        process = FakeProcess(['error\n'], code=2)
        executor = FakeExecutor(process)
        output = []
        self.assertFalse(executor.run_in_namespace('fail', output.append))
        self.assertEqual(executor.started, [('fail', True)])
        self.assertEqual(output, ['error'])

    def test_output_returns_joined_output_only_on_success(self):
        executor = FakeExecutor(FakeProcess(['one\n', 'two\n']))
        self.assertEqual(executor.output('ok'), 'one\ntwo')
        executor.process = FakeProcess(['reason\n'], code=1)
        self.assertIsNone(executor.output('fail'))

    def test_executor_process_calls_exit_callback_only_once(self):
        callback = Mock()
        process = Mock()
        process.stdout = ['line']
        process.wait.return_value = 0
        process.poll.return_value = 0
        wrapped = rootless.ExecutorProcess(process, on_exit=callback)
        self.assertEqual(wrapped.wait(), 0)
        self.assertEqual(wrapped.poll(), 0)
        callback.assert_called_once_with()

    def test_cancel_marks_process_and_stops_process_group(self):
        process = Mock()
        process.stdout = []
        process.pid = 123
        process.poll.side_effect = [None, None, 0]
        process.wait.return_value = 0
        wrapped = rootless.ExecutorProcess(process)
        with patch.object(rootless.os, 'killpg') as killpg:
            wrapped.terminate()
        self.assertTrue(wrapped.canceled)
        killpg.assert_called_once_with(123, rootless.signal.SIGTERM)
        process.wait.assert_called_once_with(timeout=10)

    def test_local_executor_filesystem_uses_direct_operations(self):
        executor = rootless.LocalExecutor()
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary, 'created'))
            executor.filesystem({'mkdir': [path]})
            result = executor.filesystem({'list': [path]})
            self.assertTrue(Path(path).is_dir())
            self.assertEqual(result['list'][path], [])

    def test_tar_extraction_script_rejects_paths_outside_destination(self):
        executor = FakeExecutor()
        self.assertTrue(rootless.extract_tarball('/tmp/stage.tar.xz', '/tmp/root', lambda _: None, executor=executor))
        script, namespace = executor.started[0]
        self.assertTrue(namespace)
        self.assertIn('tarfile.OutsideDestinationError', script)
        self.assertIn('os.path.commonpath', script)
        self.assertIn('member.ischr() or member.isblk()', script)


if __name__ == '__main__':
    unittest.main()
