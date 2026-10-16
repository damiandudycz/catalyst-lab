"""SSH transport behavior with subprocess and filesystem effects isolated."""
from io import BytesIO
from pathlib import Path
import shlex
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

from catalystlab.ssh_connection import SSHConnection, quote


class SSHCommandTests(unittest.TestCase):
    def test_command_has_master_and_batch_modes(self):
        connection = SSHConnection('192.0.2.5', port=2222, user='admin')
        master = connection._command(master=True)
        regular = connection._command('uname -a')
        self.assertEqual(master[:3], ['ssh', '-p', '2222'])
        self.assertIn('ControlMaster=yes', master)
        self.assertIn('ControlPersist=no', master)
        self.assertIn('admin@192.0.2.5', master)
        self.assertIn('ControlMaster=no', regular)
        self.assertIn('BatchMode=yes', regular)
        self.assertEqual(regular[-2:], ['admin@192.0.2.5', 'uname -a'])
        self.assertEqual(shlex.split(quote("a path/it's")), ["a path/it's"])

    def test_password_connect_uses_askpass_environment_then_removes_script(self):
        connection = SSHConnection('host')
        process = Mock()
        process.poll.return_value = None
        with patch('catalystlab.ssh_connection.subprocess.Popen', return_value=process) as popen, \
             patch('catalystlab.ssh_connection.os.path.exists', return_value=True), \
             patch.object(connection, '_check', return_value=True), \
             patch('catalystlab.ssh_connection.time.sleep'):
            self.assertIsNone(connection.connect('secret-password'))
        self.assertTrue(connection.connected)
        command, = popen.call_args.args
        self.assertNotIn('secret-password', command)
        environment = popen.call_args.kwargs['env']
        self.assertEqual(environment['CATALYSTLAB_SSH_PASSWORD'], 'secret-password')
        askpass = environment['SSH_ASKPASS']
        self.assertFalse(Path(askpass).exists())
        self.assertIn('NumberOfPasswordPrompts=1', command)

    def test_connect_failure_returns_last_error_line(self):
        connection = SSHConnection('host')
        process = Mock()
        process.poll.return_value = 255
        process.stderr.read.return_value = 'first line\nPermission denied\n'
        with patch('catalystlab.ssh_connection.subprocess.Popen', return_value=process), \
             patch('catalystlab.ssh_connection.time.sleep'):
            error = connection.connect(None)
        self.assertEqual(error, 'Permission denied')
        self.assertFalse(connection.connected)

    def test_connect_timeout_terminates_master(self):
        connection = SSHConnection('host')
        process = Mock()
        process.poll.return_value = None
        with patch('catalystlab.ssh_connection.subprocess.Popen', return_value=process), \
             patch('catalystlab.ssh_connection.os.path.exists', return_value=False), \
             patch('catalystlab.ssh_connection.time.monotonic', side_effect=[0, 61]), \
             patch('catalystlab.ssh_connection.time.sleep'):
            error = connection.connect(None)
        self.assertEqual(error, 'Connection to host timed out')
        process.terminate.assert_called_once()

    def test_close_exits_control_master_and_terminates_child(self):
        connection = SSHConnection('host')
        connection.connected = True
        connection._master = Mock()
        connection._master.poll.return_value = None
        with patch('catalystlab.ssh_connection.subprocess.run') as run:
            connection.close()
        self.assertEqual(run.call_args.args[0][1:4], ['-o', f'ControlPath={connection.control_path}', '-O'])
        self.assertEqual(run.call_args.args[0][4], 'exit')
        connection._master.terminate.assert_called_once()
        self.assertFalse(connection.connected)

    def test_close_when_not_connected_is_a_noop(self):
        connection = SSHConnection('host')
        with patch('catalystlab.ssh_connection.subprocess.run') as run:
            connection.close()
        run.assert_not_called()


class SSHCommandExecutionTests(unittest.TestCase):
    def test_run_streams_output_and_returns_exit_status(self):
        connection = SSHConnection('host')
        process = Mock()
        process.stdout = ['first\n', 'second\n']
        process.wait.return_value = 0
        holder, output = [], []
        with patch.object(connection, 'start', return_value=process) as start:
            self.assertTrue(connection.run('echo ok', output.append, holder))
        start.assert_called_once_with('echo ok')
        self.assertEqual(output, ['first', 'second'])
        self.assertEqual(holder, [process])

    def test_output_raises_last_remote_error_or_generic_error(self):
        connection = SSHConnection('host')
        with patch.object(connection, 'run', side_effect=lambda _script, callback: (callback('failed'), False)[1]):
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                connection.output('false')
        with patch.object(connection, 'run', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'Command failed on remote machine'):
                connection.output('false')

    def test_stream_file_sends_bytes_reports_fraction_and_collects_remote_output(self):
        connection = SSHConnection('host')
        class CaptureInput:
            def __init__(self):
                self.data = bytearray()
            def write(self, value):
                self.data.extend(value)
                return len(value)
            def close(self):
                pass
        process = Mock()
        process.stdin = CaptureInput()
        process.stdout = [b'Extracting\n']
        process.wait.return_value = 0
        output, progress, holder = [], [], []
        with tempfile.NamedTemporaryFile() as file:
            file.write(b'x' * (2 * 1024 * 1024 + 17))
            file.flush()
            with patch('catalystlab.ssh_connection.subprocess.Popen', return_value=process):
                self.assertTrue(connection.stream_file(file.name, 'tar -x', output.append, progress.append, holder))
        self.assertEqual(output, ['Extracting'])
        self.assertEqual(progress[-1], 1.0)
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(holder, [process])
        self.assertEqual(len(process.stdin.data), 2 * 1024 * 1024 + 17)

    def test_stream_file_handles_empty_file_and_nonzero_remote_exit(self):
        connection = SSHConnection('host')
        process = Mock()
        process.stdin = BytesIO()
        process.stdout = []
        process.wait.return_value = 1
        progress = []
        with tempfile.NamedTemporaryFile() as file, patch('catalystlab.ssh_connection.subprocess.Popen', return_value=process):
            self.assertFalse(connection.stream_file(file.name, 'cat', progress_handler=progress.append))
        self.assertEqual(progress, [])


if __name__ == '__main__':
    unittest.main()
