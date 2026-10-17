import unittest
from types import SimpleNamespace

from catalystlab.architecture import Architecture
from catalystlab.packages_directory import PackagesDirectory
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


if __name__ == "__main__":
    unittest.main()
