"""Lima configuration compatibility checks."""
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import lima


class BuildMachineConfigTests(unittest.TestCase):
    def test_generated_config_does_not_set_deprecated_top_level_rosetta(self):
        configuration = lima.machine_configuration(4, 8, ['/library/Cache'])

        self.assertNotIn('\nrosetta:\n', configuration)
        self.assertIn('base:\n- template:_images/alpine-3.23', configuration)


if __name__ == '__main__':
    unittest.main()
