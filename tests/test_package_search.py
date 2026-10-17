import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if "catalystlab" not in sys.modules:
    package = types.ModuleType("catalystlab")
    package.__path__ = [str(ROOT / "src")] + [str(path) for path in (ROOT / "src").rglob("*") if path.is_dir()]
    sys.modules["catalystlab"] = package

from catalystlab.package_search import filter_package_categories, package_version_key


class PackageSearchTests(unittest.TestCase):
    def setUp(self):
        self.packages = {
            "sys-kernel": {"gentoo-sources": ["6.8", "6.12"], "raspberrypi-sources": ["6.6"]},
            "app-editors": {"vim": ["9.1"], "emacs": ["30"]},
        }

    def test_short_query_returns_all_categories(self):
        self.assertIs(filter_package_categories(self.packages, " g ", minimum=2), self.packages)

    def test_category_match_keeps_all_its_packages(self):
        self.assertEqual(filter_package_categories(self.packages, "kernel"),
                         {"sys-kernel": self.packages["sys-kernel"]})

    def test_package_match_filters_within_category(self):
        self.assertEqual(filter_package_categories(self.packages, "vim"),
                         {"app-editors": {"vim": ["9.1"]}})

    def test_full_path_search_matches_category_and_package(self):
        self.assertEqual(filter_package_categories(self.packages, "sys-kernel/rasp", match_full_paths=True),
                         {"sys-kernel": {"raspberrypi-sources": ["6.6"]}})

    def test_version_key_orders_numeric_parts(self):
        self.assertEqual(sorted(["6.12.16", "6.6.62"], key=package_version_key), ["6.6.62", "6.12.16"])


if __name__ == "__main__":
    unittest.main()
