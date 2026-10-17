from pathlib import Path
import os
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import project_stage_cache


class StageCachePathDisplayTests(unittest.TestCase):
    def test_home_paths_use_tilde_and_external_paths_stay_absolute(self):
        home = os.path.expanduser('~')
        self.assertEqual(project_stage_cache.display_path(os.path.join(home, 'CatalystLab')), '~/CatalystLab')
        self.assertEqual(project_stage_cache.display_path('/tmp/cache'), '/tmp/cache')


if __name__ == '__main__':
    unittest.main()
