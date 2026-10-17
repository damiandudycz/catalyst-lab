"""Library folders mounted in build machines."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import build_machine_manager


class SharedPathsTests(unittest.TestCase):
    def test_shared_paths_drop_nested_locations_but_keep_siblings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            locations = {
                'Toolsets': root / 'Cache',
                'Snapshots': root / 'Cache' / 'Extracted',
                'Releng': root / 'Projects',
                'Overlays': root / 'Projects' / 'Overlay',
                'Projects': root / 'Projects',
                'Builds': root / 'Cache-old',
                'Cache': root / 'Cache',
                'Temporary': root / 'Temporary',
                'Packages': root / 'Packages',
            }
            created = []
            with patch.object(build_machine_manager, 'library_path',
                              side_effect=lambda name: str(locations[name])), \
                    patch.object(build_machine_manager.os, 'makedirs',
                                 side_effect=lambda path, exist_ok: created.append(path)):
                paths = build_machine_manager.shared_paths()

        expected = {str((root / name).resolve()) for name in ('Cache', 'Projects', 'Cache-old', 'Temporary', 'Packages')}
        self.assertEqual(set(paths), expected)
        self.assertEqual(set(created), expected)


if __name__ == '__main__':
    unittest.main()
