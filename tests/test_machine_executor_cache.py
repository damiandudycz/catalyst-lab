"""Build machine caches respect Library storage and Linux filesystem needs."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.rootless import MachineExecutor, extracted_directory
from catalystlab.repository import Repository


class MachineExecutorCacheTests(unittest.TestCase):
    def test_cache_uses_configured_library_cache_path(self):
        configured_cache = '/Volumes/Library/Cache'
        settings = types.SimpleNamespace(cache_location=configured_cache)
        with patch.object(Repository, 'Settings', types.SimpleNamespace(value=settings), create=True):
            executor = MachineExecutor(types.SimpleNamespace())
            self.assertEqual(executor.cache_directory(), configured_cache)
            self.assertEqual(extracted_directory('toolsets', executor),
                             '/var/lib/catalystlab/Temporary/Work/Extracted/Toolsets')

if __name__ == '__main__':
    unittest.main()
