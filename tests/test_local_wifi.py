"""Connected Wi-Fi suggestions tolerate unavailable tools and preserve SSID punctuation."""
import sys
from pathlib import Path
import types
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(p) for p in (ROOT/'src').rglob('*') if p.is_dir()]
    sys.modules['catalystlab'] = package
from catalystlab import deploy_system as system

class LocalWifiTests(unittest.TestCase):
    def result(self, text):
        return types.SimpleNamespace(stdout=text, returncode=0)

    def test_mac_detects_wifi_interface(self):
        with patch.object(system.sys, 'platform', 'darwin'), patch.dict(sys.modules, {'CoreWLAN': None}), patch.object(system.subprocess, 'run', side_effect=[
            self.result('Hardware Port: Wi-Fi\nDevice: en1\nEthernet Address: xx\n'),
            self.result('Current Wi-Fi Network: Home: 5GHz\n')]) as run:
            self.assertEqual(system.local_wifi_ssid(), 'Home: 5GHz')
            self.assertEqual(run.call_args.args[0][-1], 'en1')

    def test_linux_networkmanager_unescapes_ssid(self):
        with patch.object(system.sys, 'platform', 'linux'), patch.object(system.subprocess, 'run', side_effect=[FileNotFoundError(), self.result('*:Home\\: 5GHz\n')]):
            self.assertEqual(system.local_wifi_ssid(), 'Home: 5GHz')

    def test_missing_tools_returns_no_suggestion(self):
        with patch.object(system.sys, 'platform', 'linux'), patch.object(system.subprocess, 'run', side_effect=FileNotFoundError()):
            self.assertIsNone(system.local_wifi_ssid())

class NativeWifiTests(unittest.TestCase):
    def test_native_permission_path_is_used_when_requested(self):
        with patch.object(system.sys, 'platform', 'darwin'), patch.object(system, '_mac_wifi_ssid', return_value='Home Wi-Fi') as native, patch.object(system.subprocess, 'run') as run:
            self.assertEqual(system.local_wifi_ssid(request_permission=True), 'Home Wi-Fi')
            native.assert_called_once_with()
            run.assert_not_called()

    def test_permission_is_not_requested_for_passive_lookup(self):
        with patch.object(system.sys, 'platform', 'darwin'), patch.object(system, '_mac_wifi_ssid') as native, patch.dict(sys.modules, {'CoreWLAN': None}), patch.object(system.subprocess, 'run', side_effect=FileNotFoundError()):
            self.assertIsNone(system.local_wifi_ssid())
            native.assert_not_called()
