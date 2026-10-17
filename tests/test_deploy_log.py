import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if "catalystlab" not in sys.modules:
    package = types.ModuleType("catalystlab")
    package.__path__ = [str(ROOT / "src")] + [str(path) for path in (ROOT / "src").rglob("*") if path.is_dir()]
    sys.modules["catalystlab"] = package

from catalystlab.deploy_log import write_log_line


class DeployLogTests(unittest.TestCase):
    def test_writes_lines_and_reuses_open_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "deploy.log")
            file = write_log_line(path, None, "first")
            same_file = write_log_line(path, file, "second")
            file.close()

            self.assertIs(same_file, file)
            self.assertEqual(path.read_text(), "first\nsecond\n")

    def test_missing_path_does_not_open_file(self):
        self.assertIsNone(write_log_line(None, None, "ignored"))


if __name__ == "__main__":
    unittest.main()
