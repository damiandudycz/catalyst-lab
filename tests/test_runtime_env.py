"""Runtime environment selection and Flatpak host paths."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.runtime_env import RuntimeEnv


class RuntimeEnvironmentTests(unittest.TestCase):
    def test_current_selects_host_or_flatpak_from_environment(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertIs(RuntimeEnv.current(), RuntimeEnv.HOST)
        with patch.dict('os.environ', {'FLATPAK_ID': 'org.example.App'}, clear=True):
            self.assertIs(RuntimeEnv.current(), RuntimeEnv.FLATPAK)

    def test_flatpak_paths_use_host_bridge_only_when_needed(self):
        with patch('catalystlab.runtime_env.os.path.exists', side_effect=lambda path: path == '/run/host/etc'):
            self.assertEqual(RuntimeEnv.FLATPAK.resolve_path_for_host_access('/etc/os-release'),
                             '/run/host/etc/os-release')
        with patch('catalystlab.runtime_env.os.path.exists', return_value=True):
            self.assertEqual(RuntimeEnv.FLATPAK.resolve_path_for_host_access('/etc/os-release'), '/etc/os-release')
        self.assertEqual(RuntimeEnv.HOST.resolve_path_for_host_access('/etc/os-release'), '/etc/os-release')


if __name__ == '__main__':
    unittest.main()
