"""Kernel package discovery follows the snapshot selected for the build."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import project_kernel_packages as kernels


class KernelPackageDiscoveryTests(unittest.TestCase):
    def test_explicit_snapshot_overrides_project_default(self):
        project_snapshot = types.SimpleNamespace(filename='project.sqfs', file_path=lambda: '/snapshots/project.sqfs')
        selected_snapshot = types.SimpleNamespace(filename='selected.sqfs', file_path=lambda: '/snapshots/selected.sqfs')
        project = types.SimpleNamespace(get_snapshot=Mock(return_value=project_snapshot))
        kind = kernels.KernelPackageKind.DISTRIBUTION

        with patch.object(kernels.os.path, 'isfile', return_value=True), \
             patch.object(kernels, '_snapshot_packages', return_value={'sys-kernel/custom': kind}) as read_snapshot, \
             patch.object(kernels, '_stage_overlays', return_value=[]):
            packages = kernels.load_kernel_packages(project, object(), snapshot=selected_snapshot)

        self.assertEqual([item.atom for item in packages], ['sys-kernel/custom'])
        read_snapshot.assert_called_once_with('/snapshots/selected.sqfs')
        project.get_snapshot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
