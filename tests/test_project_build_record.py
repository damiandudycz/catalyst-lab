"""Build history reads only the log tail shown in the UI."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import project_build_record


class ProjectBuildRecordTests(unittest.TestCase):
    def test_stage_log_keeps_only_recent_lines_and_marks_omitted_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary, 'build.log')
            log_path.write_text('first\nsecond\nthird\nfourth\n')
            with (
                patch.object(project_build_record, '_MAX_LOG_LINES', 2),
                patch.object(project_build_record, 'stage_builds_directory', return_value=temporary),
            ):
                lines = project_build_record._stage_log(None, 'caffbdf2-a630-5c62-a7b3-bd156c6307e9', '')

        self.assertEqual(lines, [f'… 2 earlier lines are in {log_path}', 'third', 'fourth'])

    def test_missing_run_record_does_not_reconstruct_steps_from_stage_builds(self):
        project = types.SimpleNamespace(name='Test project')
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(project_build_record, 'build_records_directory', return_value=temporary):
            recorded = project_build_record.load_build_record(project, '20261017T000000Z')
            failed = project_build_record.failed_steps(project, '20261017T000000Z')

        self.assertEqual(recorded.title, 'Building Test project')
        self.assertEqual(recorded.stages[0].name, 'Run details unavailable')
        self.assertEqual(failed, [])

    def test_malformed_run_record_has_no_failed_steps(self):
        project = object()
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(project_build_record, 'build_records_directory', return_value=temporary):
            Path(temporary, 'broken.json').write_text('{')
            self.assertEqual(project_build_record.failed_steps(project, 'broken'), [])


if __name__ == '__main__':
    unittest.main()
