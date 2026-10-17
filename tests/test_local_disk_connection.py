"""Local disk deployment keeps root authorization only while connected."""
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if "catalystlab" not in sys.modules:
    package = types.ModuleType("catalystlab")
    package.__path__ = [str(ROOT / "src")] + [str(path) for path in (ROOT / "src").rglob("*") if path.is_dir()]
    sys.modules["catalystlab"] = package

from catalystlab.local_disk_connection import LocalDiskConnection


class LocalDiskConnectionTests(unittest.TestCase):
    def test_connect_retains_authorization_until_close(self):
        keeper = Mock()
        client = Mock()
        client.authorize_and_run.side_effect = lambda **kwargs: kwargs["callback"](keeper)
        connection = LocalDiskConnection()

        with patch("catalystlab.local_disk_connection.RootHelperClient.shared", return_value=client):
            self.assertIsNone(connection.connect())
            self.assertTrue(connection.connected)
            connection.close()

        keeper.retain.assert_called_once_with()
        keeper.release.assert_called_once_with()
        self.assertFalse(connection.connected)

    def test_cancelled_authorization_is_released(self):
        keeper = Mock()
        client = Mock()
        cancel = threading.Event()
        cancel.set()
        client.authorize_and_run.side_effect = lambda **kwargs: kwargs["callback"](keeper)
        connection = LocalDiskConnection()

        with patch("catalystlab.local_disk_connection.RootHelperClient.shared", return_value=client):
            self.assertEqual(connection.connect(cancel_event=cancel), "Canceled")

        keeper.retain.assert_not_called()
        keeper.release.assert_called_once_with()
        self.assertFalse(connection.connected)

    def test_missing_authorization_returns_actionable_error(self):
        client = Mock()
        client.authorize_and_run.side_effect = lambda **kwargs: kwargs["callback"](None)
        connection = LocalDiskConnection()

        with patch("catalystlab.local_disk_connection.RootHelperClient.shared", return_value=client):
            self.assertEqual(connection.connect(), "Root access is needed to install on disks of this computer")

        self.assertFalse(connection.connected)


if __name__ == "__main__":
    unittest.main()
