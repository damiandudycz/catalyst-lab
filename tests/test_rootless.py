"""Rootless executor contracts and safe local filesystem operations."""
from pathlib import Path
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

from catalystlab import rootless


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
