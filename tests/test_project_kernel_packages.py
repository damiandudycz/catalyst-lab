"""Kernel package discovery follows the snapshot selected for the build."""
from pathlib import Path
import sys
import os
import tempfile
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

    def test_classifies_distribution_sources_and_unsupported_ebuilds(self):
        cases = (
            ('inherit kernel-build', kernels.KernelPackageKind.DISTRIBUTION),
            ('inherit kernel-install', kernels.KernelPackageKind.DISTRIBUTION),
            ('inherit kernel-2', kernels.KernelPackageKind.SOURCES),
            ('inherit kernel-2\nETYPE="headers"', kernels.KernelPackageKind.UNSUPPORTED),
            ('inherit linux-info', kernels.KernelPackageKind.UNSUPPORTED),
        )

        for ebuild, expected in cases:
            with self.subTest(ebuild=ebuild):
                self.assertEqual(kernels.kernel_package_kind(ebuild), expected)

    def test_package_kind_matches_versioned_portage_atoms(self):
        packages = [
            kernels.KernelPackage('sys-kernel/gentoo-kernel', kernels.KernelPackageKind.DISTRIBUTION),
            kernels.KernelPackage('sys-kernel/gentoo-kernel-bin', kernels.KernelPackageKind.UNSUPPORTED),
        ]

        self.assertEqual(
            kernels.package_kind('=sys-kernel/gentoo-kernel-6.12.8:6.12::gentoo', packages),
            kernels.KernelPackageKind.DISTRIBUTION,
        )
        self.assertEqual(
            kernels.package_kind('sys-kernel/gentoo-kernel-bin-6.12.8', packages),
            kernels.KernelPackageKind.UNSUPPORTED,
        )
        self.assertIsNone(kernels.package_kind('sys-kernel/unknown-kernel', packages))
        self.assertIsNone(kernels.package_kind(None, packages))

    def test_derived_distkernel_only_for_known_kernel_types(self):
        packages = [
            kernels.KernelPackage('sys-kernel/dist', kernels.KernelPackageKind.DISTRIBUTION),
            kernels.KernelPackage('sys-kernel/sources', kernels.KernelPackageKind.SOURCES),
        ]

        self.assertIs(kernels.derived_distkernel('sys-kernel/dist', packages), True)
        self.assertIs(kernels.derived_distkernel('sys-kernel/sources', packages), False)
        self.assertIsNone(kernels.derived_distkernel('sys-kernel/unknown', packages))

    def test_overlay_kernel_replaces_snapshot_classification(self):
        snapshot = types.SimpleNamespace(filename='gentoo.sqfs', file_path=lambda: '/snapshots/gentoo.sqfs')
        project = types.SimpleNamespace(get_snapshot=Mock(return_value=snapshot))
        sources_kind = kernels.KernelPackageKind.SOURCES
        unsupported_kind = kernels.KernelPackageKind.UNSUPPORTED

        with patch.object(kernels.os.path, 'isfile', return_value=True), \
             patch.object(kernels, '_snapshot_packages', return_value={
                 'sys-kernel/custom': unsupported_kind,
                 'sys-kernel/gentoo-kernel': kernels.KernelPackageKind.DISTRIBUTION,
             }), \
             patch.object(kernels, '_stage_overlays', return_value=[('my-overlay', '/overlay')]), \
             patch.object(kernels, '_tree_state', return_value=1), \
             patch.object(kernels, '_folder_packages', return_value={
                 'sys-kernel/custom': sources_kind,
             }):
            packages = kernels.load_kernel_packages(project, object())

        by_atom = {package.atom: package for package in packages}
        self.assertEqual(by_atom['sys-kernel/custom'].kind, sources_kind)
        self.assertEqual(by_atom['sys-kernel/custom'].repositories, ['gentoo', 'my-overlay'])
        self.assertEqual(by_atom['sys-kernel/gentoo-kernel'].repositories, ['gentoo'])

    def test_newest_ebuild_is_selected_for_each_package(self):
        names = [
            'sys-kernel/gentoo-kernel/gentoo-kernel-6.1.ebuild',
            'sys-kernel/gentoo-kernel/gentoo-kernel-6.12.ebuild',
            'sys-kernel/raspberrypi-sources/raspberrypi-sources-6.1.ebuild',
            'sys-kernel/readme.txt',
        ]

        self.assertEqual(kernels._newest_ebuilds(names), {
            'sys-kernel/gentoo-kernel': 'sys-kernel/gentoo-kernel/gentoo-kernel-6.12.ebuild',
            'sys-kernel/raspberrypi-sources': 'sys-kernel/raspberrypi-sources/raspberrypi-sources-6.1.ebuild',
        })

    def test_overlay_cache_state_changes_when_ebuild_contents_change(self):
        with tempfile.TemporaryDirectory() as directory:
            ebuild = Path(directory, 'sys-kernel', 'gentoo-kernel', 'gentoo-kernel-6.12.ebuild')
            ebuild.parent.mkdir(parents=True)
            ebuild.write_text('inherit kernel-build')
            before = kernels._tree_state(directory)
            stat = ebuild.stat()
            os.utime(ebuild, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

            self.assertNotEqual(before, kernels._tree_state(directory))


if __name__ == '__main__':
    unittest.main()
