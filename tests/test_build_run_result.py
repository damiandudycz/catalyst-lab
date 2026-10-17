"""Build-run status aggregation and newest-run selection."""
import ast
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
import unittest


class BuildRunResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "src/objects/project/project_build.py"
        tree = ast.parse(path.read_text())
        nodes = [node for node in tree.body if
                 isinstance(node, ast.ClassDef) and node.name in {"BuildRunResult", "StageBuildStatus"} or
                 isinstance(node, ast.FunctionDef) and node.name == "last_build_run"]
        namespace = {"Enum": Enum, "load_project_builds": lambda _project: []}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
        cls.namespace = namespace

    def test_latest_build_record_selects_newest_run_and_aggregates_its_stages(self):
        status = self.namespace["StageBuildStatus"]
        builds = [SimpleNamespace(timestamp="new", date=2, status=status.COMPLETED),
                  SimpleNamespace(timestamp="new", date=1, status=status.COMPLETED),
                  SimpleNamespace(timestamp="old", date=0, status=status.FAILED)]
        self.namespace["load_project_builds"] = lambda _project: builds
        self.assertEqual(self.namespace["last_build_run"](object()), ("new", self.namespace["BuildRunResult"].COMPLETED))

    def test_failed_stage_takes_precedence_over_skipped_or_stopped_stages(self):
        status = self.namespace["StageBuildStatus"]
        builds = [SimpleNamespace(timestamp="run", date=1, status=status.STOPPED),
                  SimpleNamespace(timestamp="run", date=0, status=status.SKIPPED)]
        self.namespace["load_project_builds"] = lambda _project: builds
        self.assertEqual(self.namespace["last_build_run"](object()),
                         ("run", self.namespace["BuildRunResult"].FAILED))

    def test_active_run_is_not_reported_as_stopped(self):
        status = self.namespace["StageBuildStatus"]
        builds = [SimpleNamespace(timestamp="run", date=1, status=status.IN_PROGRESS)]
        self.namespace["load_project_builds"] = lambda _project: builds
        self.assertEqual(self.namespace["last_build_run"](object(), running_timestamp="run"),
                         ("run", self.namespace["BuildRunResult"].COMPLETED))


if __name__ == "__main__":
    unittest.main()
