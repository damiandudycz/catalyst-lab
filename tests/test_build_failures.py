"""Build failure identity and resolution compatibility."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import types
import uuid
import unittest
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
if "catalystlab" not in sys.modules:
    package = types.ModuleType("catalystlab")
    package.__path__ = [str(ROOT / "src")] + [str(path) for path in (ROOT / "src").rglob("*") if path.is_dir()]
    sys.modules["catalystlab"] = package

from catalystlab.build_failures import PackageFailure, _reconcile_resolution_states, package_atom
from catalystlab.project_build import StageBuildStatus


class BuildFailureTests(unittest.TestCase):
    def test_package_atom_removes_version_and_revision(self):
        self.assertEqual(package_atom("sys-libs/readline-8.3_p6-r1"), "sys-libs/readline")

    def test_resolution_status_accepts_current_and_legacy_values(self):
        item = {"resolution": "potentially_solved", "fixed": False}
        failure = PackageFailure("sys-apps/pkg-1", "gentoo", "compile", None, None,
                                 types.SimpleNamespace(details={"failure": {"packages": [item]}}), 0)
        self.assertEqual(failure.resolution_status, "potentially_solved")

        item["resolution"] = "unknown"
        self.assertEqual(failure.resolution_status, "open")
        item["resolution"] = []
        self.assertEqual(failure.resolution_status, "open")
        item.pop("resolution")
        item["fixed"] = True
        self.assertEqual(failure.resolution_status, "solved")

    def test_matching_successful_build_marks_previous_failure_potentially_solved(self):
        with TemporaryDirectory() as directory:
            project_id, stage_id = uuid.uuid4(), uuid.uuid4()
            builds = []
            saved = []

            class Build:
                def __init__(self, timestamp, status, details):
                    self.id = uuid.uuid4()
                    self.stage_id = stage_id
                    self.stage_name = "stage4"
                    self.timestamp = timestamp
                    self.date = datetime.fromisoformat(timestamp)
                    self.status = status
                    self.details = details
                    self.path = str(Path(directory) / timestamp)
                    Path(self.path).mkdir()
                    (Path(self.path) / "stage.spec").write_text(
                        f"target: stage4\ntimestamp: {timestamp}\n", encoding="utf-8")

                def save(self, project):
                    saved.append((self, project))

            config = {"inputs": {"toolset": {"catalyst": "test-version"}}}
            failed = Build("2026-01-01T00:00:00", StageBuildStatus.FAILED, {
                **config,
                "failure": {
                    "reason": "compiler error",
                    "packages": [{"package": "sys-apps/pkg-1", "repository": "gentoo", "phase": "compile"}],
                },
            })
            completed = Build("2026-01-02T00:00:00", StageBuildStatus.COMPLETED, config.copy())
            builds.extend((failed, completed))
            project = type("Project", (), {"id": project_id})()

            _reconcile_resolution_states(project, builds)

            item = failed.details["failure"]["packages"][0]
            self.assertEqual(item["resolution"], "potentially_solved")
            self.assertTrue(failed.details["bug_resolution_processed"])
            self.assertTrue(completed.details["bug_resolution_processed"])
            self.assertEqual({build.id for build, _ in saved}, {failed.id, completed.id})


if __name__ == "__main__":
    unittest.main()
