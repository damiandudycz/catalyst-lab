import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if "catalystlab" not in sys.modules:
    package = types.ModuleType("catalystlab")
    package.__path__ = [str(ROOT / "src")] + [str(path) for path in (ROOT / "src").rglob("*") if path.is_dir()]
    sys.modules["catalystlab"] = package

from catalystlab.formatters import format_size


class SizeFormattingTests(unittest.TestCase):
    def test_formats_bytes_and_scales_units(self):
        self.assertEqual(format_size(512), "512 B")
        self.assertEqual(format_size(1536), "1.5 KB")

    def test_respects_maximum_unit(self):
        self.assertEqual(format_size(1024 ** 4), "1.0 TB")
        self.assertEqual(format_size(1024 ** 4, max_unit="GB"), "1024.0 GB")


if __name__ == "__main__":
    unittest.main()
