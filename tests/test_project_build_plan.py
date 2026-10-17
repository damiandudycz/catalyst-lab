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

from catalystlab.project_build import StageBuild, StageBuildMode, StageBuildPlan, StageBuildStatus


class StageBuildPlanTests(unittest.TestCase):
    def test_builds_missing_ancestors_before_selected_stage(self):
        root_id, parent_id, selected_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        root = types.SimpleNamespace(id=root_id, name='stage1', parent=None)
        parent = types.SimpleNamespace(id=parent_id, name='stage3', parent=root_id)
        selected = types.SimpleNamespace(id=selected_id, name='stage4', parent=parent_id)
        project = types.SimpleNamespace(stages=[selected, parent, root])

        plan = StageBuildPlan(project, {selected_id}, builds=[])

        self.assertEqual([stage.id for stage in plan.build_order()], [root_id, parent_id, selected_id])
        self.assertEqual(plan.entries[root_id].mode, StageBuildMode.REQUIRED)
        self.assertEqual(plan.entries[parent_id].mode, StageBuildMode.REQUIRED)

    def test_build_order_reports_parent_cycles_instead_of_recursing_forever(self):
        first_id, second_id = uuid.uuid4(), uuid.uuid4()
        first = types.SimpleNamespace(id=first_id, name='first', parent=second_id)
        second = types.SimpleNamespace(id=second_id, name='second', parent=first_id)
        project = types.SimpleNamespace(stages=[first, second])

        plan = StageBuildPlan(project, {first_id}, builds=[])

        with self.assertRaisesRegex(ValueError, 'Stage parent cycle includes'):
            plan.build_order()

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
