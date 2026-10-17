"""Root helper request failures stop only a broken active connection."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import root_function, root_helper_client
from catalystlab.root_helper_server import ServerCommand


class RootHelperRequestTests(unittest.TestCase):
    def client(self, running):
        client = root_helper_client.RootHelperClient.__new__(root_helper_client.RootHelperClient)
        client._is_server_process_running = running
        client.ensure_server_ready = Mock(return_value=False)
        client.stop_root_helper = Mock()
        return client

    def test_failed_active_request_stops_server(self):
        client = self.client(running=True)

        with self.assertRaises(root_helper_client.ServerCallError):
            client.send_request(ServerCommand.PING)

        client.stop_root_helper.assert_called_once_with(instant=True)

    def test_failed_handshake_or_exit_does_not_stop_server(self):
        for request in (ServerCommand.HANDSHAKE, ServerCommand.EXIT):
            with self.subTest(request=request):
                client = self.client(running=True)
                with self.assertRaises(root_helper_client.ServerCallError):
                    client.send_request(request)
                client.stop_root_helper.assert_not_called()

    def test_root_function_variants_share_call_options(self):
        function = lambda: None
        proxy = root_function.root_function(function)
        root_function.ROOT_FUNCTION_REGISTRY.pop(function.__name__)
        client = Mock()
        with patch.object(root_helper_client.RootHelperClient, 'shared', return_value=client):
            proxy._async_raw(handler='output', completion_handler='done', value=1)

        client.call_root_function.assert_called_once_with(
            function.__name__, handler='output', asynchronous=True, raw=True,
            completion_handler='done', value=1,
        )


if __name__ == '__main__':
    unittest.main()
