import sys
import unittest
from pathlib import Path
import types

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.serial_console import SerialConsoleFilter


class SerialConsoleFilterTests(unittest.TestCase):
    def test_holds_incomplete_escape_sequences_across_chunks(self):
        console = SerialConsoleFilter()
        self.assertEqual(console.feed("boot\x1b[?25"), "boot")
        self.assertEqual(console.feed("lready\x1b[31"), "ready")
        self.assertEqual(console.feed("merror\x1b[0m"), "\x1b[31merror\x1b[0m")

    def test_removes_cursor_controls_and_normalizes_carriage_returns(self):
        console = SerialConsoleFilter()
        text = console.feed("status\x1b[2K\rready\r\nnext\x1b[3;1Hline")
        self.assertEqual(text, "status\nready\nnextline")

    def test_discards_osc_and_non_printing_control_bytes(self):
        console = SerialConsoleFilter()
        self.assertEqual(console.feed("a\x1b]0;window title\x07b\x00c"), "abc")


if __name__ == "__main__":
    unittest.main()
