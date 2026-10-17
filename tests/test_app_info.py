from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if "catalystlab" not in sys.modules:
    package = types.ModuleType("catalystlab")
    package.__path__ = [str(ROOT / "src")] + [str(path) for path in (ROOT / "src").rglob("*") if path.is_dir()]
    sys.modules["catalystlab"] = package

from catalystlab.app_info import version_tuple


class AppVersionTests(unittest.TestCase):
    def test_compares_numeric_versions_by_component(self):
        self.assertLess(version_tuple("1.9.0"), version_tuple("1.10.0"))

    def test_ignores_prerelease_suffix(self):
        self.assertEqual(version_tuple("2.0.0-beta"), (2, 0, 0))

    def test_stops_at_first_non_numeric_component(self):
        self.assertEqual(version_tuple("2.preview.1"), (2,))

    def test_stops_at_non_numeric_suffix_within_component(self):
        self.assertEqual(version_tuple("2.1rc1.4"), (2, 1))
        self.assertEqual(version_tuple("release-2.1"), ())


if __name__ == "__main__":
    unittest.main()
