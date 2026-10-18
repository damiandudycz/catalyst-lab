"""Build queue membership and ordering rules."""
import gc
from pathlib import Path
import sys
import types
import unittest
import uuid
import weakref
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if "catalystlab" not in sys.modules:
    package = types.ModuleType("catalystlab")
    package.__path__ = [str(ROOT / "src")] + [str(path) for path in (ROOT / "src").rglob("*") if path.is_dir()]
    sys.modules["catalystlab"] = package

from catalystlab import build_queue as module
from catalystlab.build_queue import BuildQueue


class BuildQueueTests(unittest.TestCase):
    def setUp(self):
        self.timeout = patch.object(module.GLib, "timeout_add", return_value=1)
        self.timeout.start()
        self.addCleanup(self.timeout.stop)
        self.queue = BuildQueue()
        self.project = types.SimpleNamespace(id=uuid.uuid4())

    def test_project_can_be_queued_once_and_entry_can_be_reordered(self):
        stages = [uuid.uuid4(), uuid.uuid4()]
        with patch.object(module, "running_build", return_value=None):
            self.queue.add(self.project, stages, "snapshot.sqfs")
            with self.assertRaisesRegex(RuntimeError, "already queued"):
                self.queue.add(self.project, stages, None)

        other = types.SimpleNamespace(id=uuid.uuid4())
        with patch.object(module, "running_build", return_value=None):
            self.queue.add(other, [uuid.uuid4()], None)
        first, second = self.queue.entries
        self.queue.move(second, -1)
        self.assertEqual(self.queue.entries, [second, first])
        self.assertEqual(first.stage_ids, stages)
        self.assertEqual(first.snapshot, "snapshot.sqfs")

    def test_cleanup_preference_is_kept_with_queued_build(self):
        with patch.object(module, "running_build", return_value=None):
            self.queue.add(self.project, [uuid.uuid4()], None, cleanup_previous_builds=True)
        self.assertTrue(self.queue.entries[0].cleanup_previous_builds)

    def test_selected_runner_is_kept_with_queued_build(self):
        runner = types.SimpleNamespace(id="host")
        with patch.object(module, "running_build", return_value=None):
            self.queue.add(self.project, [uuid.uuid4()], None, runner=runner)
        self.assertEqual(self.queue.entries[0].runner_id, "host")

    def test_project_being_built_cannot_be_queued(self):
        build = types.SimpleNamespace(project_directory=self.project)
        with patch.object(module, "running_build", return_value=build):
            with self.assertRaisesRegex(RuntimeError, "being built"):
                self.queue.add(self.project, [uuid.uuid4()], None)
        self.assertEqual(self.queue.entries, [])

    def test_observed_build_is_subscribed_once_and_not_kept_alive(self):
        class FakeBuild:
            def __init__(self):
                self.event_bus = Mock()

        build = FakeBuild()
        reference = weakref.ref(build)
        with patch.object(module, "running_build", return_value=build):
            self.queue._on_processes_changed()
            self.queue._on_processes_changed()

        build.event_bus.subscribe.assert_called_once()
        del build
        gc.collect()

        self.assertIsNone(reference())
        self.assertEqual(len(self.queue._observed), 0)


if __name__ == "__main__":
    unittest.main()
