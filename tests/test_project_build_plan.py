"""Stage build plans choose the newest supplied build records."""
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import types
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.project_build import StageBuild, StageBuildPlan, StageBuildStatus


class StageBuildPlanTests(unittest.TestCase):
    def test_latest_build_and_attempt_use_newest_records_even_when_input_is_unsorted(self):
        stage_id = uuid.uuid4()
        stage = types.SimpleNamespace(id=stage_id, name='stage4', parent=None)
        project = types.SimpleNamespace(stages=[stage])
        with tempfile.TemporaryDirectory() as temporary:
            older_path, newer_path = Path(temporary, 'older.tar'), Path(temporary, 'newer.tar')
            older_path.touch(); newer_path.touch()
            older = StageBuild(stage_id, 'stage4', 'older', date=datetime(2026, 10, 15),
                               path=temporary, artifact=older_path.name, status=StageBuildStatus.COMPLETED)
            newer = StageBuild(stage_id, 'stage4', 'newer', date=datetime(2026, 10, 16),
                               path=temporary, artifact=newer_path.name, status=StageBuildStatus.COMPLETED)

            plan = StageBuildPlan(project, set(), builds=[older, newer])
            self.assertIs(plan.latest_build(stage), newer)
            self.assertIs(plan.latest_attempt(stage), newer)


if __name__ == '__main__':
    unittest.main()
