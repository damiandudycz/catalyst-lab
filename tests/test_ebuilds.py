"""Shared ebuild path parsing for snapshots and overlays."""
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.ebuilds import group_ebuild_paths
from catalystlab.overlay_directory import OverlayDirectory


class EbuildParsingTests(unittest.TestCase):
    def test_groups_valid_paths_and_sorts_versions(self):
        self.assertEqual(group_ebuild_paths([
            'sys-kernel/kernel/kernel-6.10.ebuild',
            'dev-libs/zlib/zlib-1.3.ebuild',
            'sys-kernel/kernel/kernel-6.9.ebuild',
            'sys-kernel/kernel/not-kernel.ebuild',
            'malformed.ebuild',
        ]), {
            'dev-libs': {'zlib': ['1.3']},
            'sys-kernel': {'kernel': ['6.9', '6.10']},
        })

    def test_overlay_load_uses_the_shared_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory, 'sys-kernel', 'kernel')
            package.mkdir(parents=True)
            (package / 'kernel-6.10.ebuild').touch()
            (package / 'kernel-6.9.ebuild').touch()
            overlay = types.SimpleNamespace(directory_path=lambda: directory)
            self.assertEqual(OverlayDirectory.load_ebuilds(overlay), {
                'sys-kernel': {'kernel': ['6.9', '6.10']},
            })


if __name__ == '__main__':
    unittest.main()
