"""Runner lifecycle reservations and overlap policy."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.runner import HostRunner


class HostRunnerReservationTests(unittest.TestCase):
    def setUp(self):
        self.runner = HostRunner()

    def test_shared_operations_can_overlap_and_are_reported(self):
        with patch.object(HostRunner, 'supported', return_value=True):
            self.runner.prepare(lambda _line: None, user='Build A')
            self.runner.prepare(lambda _line: None, user='Toolset update')
        self.assertTrue(self.runner.is_used)
        self.assertEqual(self.runner.users, ['Build A', 'Toolset update'])
        self.runner.release('Build A')
        self.assertEqual(self.runner.users, ['Toolset update'])
        self.runner.release('Toolset update')
        self.assertFalse(self.runner.is_used)

    def test_exclusive_operation_rejects_other_reservations(self):
        with patch.object(HostRunner, 'supported', return_value=True):
            self.runner.prepare(lambda _line: None, user='Image operation', exclusive=True)
            with self.assertRaisesRegex(RuntimeError, 'reserved by Image operation'):
                self.runner.prepare(lambda _line: None, user='Build')
        self.runner.release('Image operation')
        self.assertFalse(self.runner.is_used)

    def test_exclusive_operation_cannot_preempt_shared_operations(self):
        with patch.object(HostRunner, 'supported', return_value=True):
            self.runner.prepare(lambda _line: None, user='Build')
            with self.assertRaisesRegex(RuntimeError, 'in use by Build'):
                self.runner.prepare(lambda _line: None, user='Exclusive operation', exclusive=True)
        self.runner.release('Build')
