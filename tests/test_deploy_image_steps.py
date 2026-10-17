"""Image save step behavior for moves and cross-device copy fallback."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import deploy_live_iso
from catalystlab.deploy_image_steps import DeployStepSaveImage
from catalystlab.deploy_step import check_tools_script


class SaveImageStepTests(unittest.TestCase):
    def test_required_tool_check_quotes_names_and_optional_hint(self):
        script = check_tools_script(['xorriso', 'tool;name'], 'install package xorriso')

        self.assertIn("for tool in xorriso 'tool;name';", script)
        self.assertIn('Missing tools:$missing (install package xorriso)', script)

    def step(self, source, destination):
        process = types.SimpleNamespace(created_image_path=source, output_path=destination)
        step = object.__new__(DeployStepSaveImage)
        step.multistage_process = process
        step.log = Mock()
        step._update_progress = Mock()
        step._cancel_event = threading.Event()
        return step

    def test_same_filesystem_move_saves_image_without_copying(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, 'working.img')
            destination = Path(temporary, 'chosen.img')
            source.write_bytes(b'image-data')
            step = self.step(str(source), str(destination))
            step.run()
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b'image-data')
            self.assertEqual(step.log.call_args.args, (f'Image: {destination}',))
            step._update_progress.assert_not_called()

    def test_cross_device_fallback_copies_reports_progress_and_removes_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, 'working.img')
            destination = Path(temporary, 'chosen.img')
            payload = b'image-data' * 100
            source.write_bytes(payload)
            step = self.step(str(source), str(destination))
            real_replace = os.replace
            calls = 0
            def move_or_fail(src, dst):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError('cross-device link')
                return real_replace(src, dst)
            with patch.object(os, 'replace', side_effect=move_or_fail):
                step.run()
            self.assertEqual(calls, 2)
            self.assertEqual(destination.read_bytes(), payload)
            self.assertFalse(source.exists())
            self.assertFalse(Path(str(destination)+'.part').exists())
            self.assertTrue(step._update_progress.called)
            self.assertEqual(step.log.call_args.args, (f'Image: {destination}',))

    def test_missing_source_fails_when_destination_differs(self):
        with tempfile.TemporaryDirectory() as temporary:
            step = self.step(str(Path(temporary, 'missing.img')), str(Path(temporary, 'chosen.img')))
            with self.assertRaisesRegex(RuntimeError, "Created image doesn't exist"):
                step.run()

    def test_missing_destination_or_same_path_only_reports_result(self):
        step = self.step('/temporary/result.img', None)
        step.run()
        step.log.assert_called_once_with('Image: /temporary/result.img')
        same = self.step('/temporary/result.img', '/temporary/result.img')
        same.run()
        same.log.assert_called_once_with('Image: /temporary/result.img')

    def test_canceled_copy_removes_partial_and_preserves_existing_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, 'working.img')
            destination = Path(temporary, 'chosen.img')
            source.write_bytes(b'image-data')
            destination.write_bytes(b'previous image')
            step = self.step(str(source), str(destination))
            step._cancel_event.set()

            with self.assertRaisesRegex(RuntimeError, 'Canceled'):
                step.copy_file(str(source), str(destination))

            self.assertFalse(Path(str(destination) + '.part').exists())
            self.assertEqual(destination.read_bytes(), b'previous image')

    def test_iso_copy_keeps_build_artifact_and_reports_saved_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, 'build.iso')
            destination = Path(temporary, 'chosen.iso')
            payload = b'iso-data' * 100
            source.write_bytes(payload)
            process = types.SimpleNamespace(prepared_iso=str(source), iso_path=str(source),
                                            output_path=str(destination))
            step = object.__new__(deploy_live_iso._StepSaveIso)
            step.multistage_process = process
            step.log = Mock()
            step._update_progress = Mock()
            step._cancel_event = threading.Event()

            step.run()

            self.assertTrue(source.exists())
            self.assertEqual(destination.read_bytes(), payload)
            self.assertTrue(step._update_progress.called)


if __name__ == '__main__':
    unittest.main()
