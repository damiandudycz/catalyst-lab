"""Cleanup runs only for successful builds and only removes successful records of that stage."""
from pathlib import Path
import sys
import types
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if "catalystlab" not in sys.modules:
    package = types.ModuleType("catalystlab")
    package.__path__ = [str(ROOT / "src")] + [str(path) for path in (ROOT / "src").rglob("*") if path.is_dir()]
    sys.modules["catalystlab"] = package
if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    class RequestException(Exception):
        pass
    requests_stub.RequestException = RequestException
    requests_stub.get = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network access in unit test"))
    sys.modules["requests"] = requests_stub

from catalystlab import project_build_process as module
from catalystlab.project_build import StageBuildStatus
from catalystlab.multistage_process import MultiStageProcessStageState


class ProjectBuildCleanupTests(unittest.TestCase):
    def _step(self, state=MultiStageProcessStageState.COMPLETED):
        stage_id = uuid.uuid4()
        current = SimpleNamespace(id=uuid.uuid4())
        build_step = SimpleNamespace(state=state, build=current)
        target_stage = SimpleNamespace(id=stage_id, name="stage4")
        process = SimpleNamespace(project_directory=object(), canceled=False, _continue_process=lambda: None)
        return module.ProjectBuildStepCleanupPreviousBuilds(target_stage, build_step, process), stage_id, current

    def test_removes_only_older_successful_builds_of_same_stage(self):
        step, stage_id, current = self._step()
        old_success = SimpleNamespace(stage_id=stage_id, id=uuid.uuid4(), status=StageBuildStatus.COMPLETED,
                                      date=__import__("datetime").datetime.now(), delete=Mock())
        failed = SimpleNamespace(stage_id=stage_id, id=uuid.uuid4(), status=StageBuildStatus.FAILED, delete=Mock())
        other_stage = SimpleNamespace(stage_id=uuid.uuid4(), id=uuid.uuid4(), status=StageBuildStatus.COMPLETED,
                                       delete=Mock())
        current_record = SimpleNamespace(stage_id=stage_id, id=current.id, status=StageBuildStatus.COMPLETED, delete=Mock())
        with patch("catalystlab.project_build.load_project_builds",
                   return_value=[old_success, failed, other_stage, current_record]), \
             patch.object(module.GLib, "idle_add", return_value=0):
            step.start()
        old_success.delete.assert_called_once_with()
        failed.delete.assert_not_called()
        other_stage.delete.assert_not_called()
        current_record.delete.assert_not_called()

    def test_does_not_clean_when_new_build_failed(self):
        step, _, _ = self._step(MultiStageProcessStageState.FAILED)
        with patch("catalystlab.project_build.load_project_builds") as load_builds, \
             patch.object(module.GLib, "idle_add", return_value=0):
            step.start()
        load_builds.assert_not_called()

    def test_stops_deleting_when_cleanup_is_canceled(self):
        step, stage_id, current = self._step()
        def old_build():
            build = SimpleNamespace(stage_id=stage_id, id=uuid.uuid4(), status=StageBuildStatus.COMPLETED,
                                    date=__import__("datetime").datetime.now(), delete=Mock())
            build.delete.side_effect = step._cancel_event.set
            return build
        first, second = old_build(), old_build()
        current_record = SimpleNamespace(stage_id=stage_id, id=current.id, status=StageBuildStatus.COMPLETED)
        with patch("catalystlab.project_build.load_project_builds",
                   return_value=[first, second, current_record]), \
             patch.object(module.GLib, "idle_add", return_value=0):
            step.start()
        first.delete.assert_called_once_with()
        second.delete.assert_not_called()
        self.assertEqual(step.state, MultiStageProcessStageState.CANCELED)


if __name__ == "__main__":
    unittest.main()
