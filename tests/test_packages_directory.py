import unittest
from types import SimpleNamespace
import uuid

from catalystlab.architecture import Architecture
from catalystlab.packages_directory import PackagesDirectory
from catalystlab.project_stage_automatic_option import StageAutomaticOption
from catalystlab.status import StatusIndicatorState


class PackagesDirectoryCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.packages = PackagesDirectory("packages", Architecture.arm64)

    @staticmethod
    def project(architecture):
        return SimpleNamespace(get_architecture=lambda: architecture, stages=[])

    def test_matching_architecture_is_usable(self):
        project = self.project(Architecture.arm64)

        self.assertEqual(self.packages.compatibility(project), [])
        self.assertTrue(self.packages.is_usable_by(project))

    def test_different_architecture_is_reported_and_not_usable(self):
        project = self.project(Architecture.amd64)

        problems = self.packages.compatibility(project)
        self.assertEqual([problem.state for problem in problems], [StatusIndicatorState.ERROR])
        self.assertFalse(self.packages.is_usable_by(project))

    def test_unstored_parent_default_does_not_look_like_a_cpu_flag_mismatch(self):
        parent_id = uuid.uuid4()
        parent = SimpleNamespace(id=parent_id, parent=None, common_flags='-O2')
        child = SimpleNamespace(id=uuid.uuid4(), parent=parent_id)
        project = SimpleNamespace(get_architecture=lambda: Architecture.arm64, stages=[parent, child])
        self.packages.cpu_flags = '-O2'

        self.assertEqual(self.packages.compatibility(project), [])

    def test_explicit_parent_inheritance_uses_parent_flags(self):
        parent_id = uuid.uuid4()
        parent = SimpleNamespace(id=parent_id, parent=None, common_flags='-O2')
        child = SimpleNamespace(id=uuid.uuid4(), parent=parent_id,
                                common_flags=StageAutomaticOption.INHERIT_FROM_PARENT)
        project = SimpleNamespace(get_architecture=lambda: Architecture.arm64, stages=[parent, child])
        self.packages.cpu_flags = '-O2'

        self.assertEqual(self.packages.compatibility(project), [])


if __name__ == "__main__":
    unittest.main()
