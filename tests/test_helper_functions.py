"""Locale-independent parsing of Gentoo snapshot timestamps."""
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.helper_functions import parse_strict_rfc_datetime


class SnapshotTimestampTests(unittest.TestCase):
    def test_parses_timestamp_and_numeric_timezone(self):
        value = parse_strict_rfc_datetime('Sat, 17 Oct 2026 12:34:56 +0530')

        self.assertEqual(value, datetime(2026, 10, 17, 12, 34, 56,
                                         tzinfo=timezone(timedelta(hours=5, minutes=30))))
        self.assertEqual(value.utcoffset().total_seconds(), 5.5 * 60 * 60)

    def test_rejects_non_rfc_shape_and_missing_timezone(self):
        for text in ('17 Oct 2026 12:34:56 +0000', 'Sat, 17 Oct 2026 12:34:56', 'Sat, 17 Oct 2026 12:34:56 UTC'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_strict_rfc_datetime(text)

    def test_does_not_change_process_locale(self):
        with patch('locale.setlocale', side_effect=AssertionError('locale must not be changed')):
            self.assertEqual(parse_strict_rfc_datetime('Sat, 17 Oct 2026 12:34:56 +0000').utcoffset().total_seconds(), 0)


if __name__ == '__main__':
    unittest.main()
