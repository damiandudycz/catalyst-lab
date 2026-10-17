"""Project build paths are translated consistently into the toolset."""
from pathlib import Path
import sys
import types
import uuid
import unittest
from unittest.mock import ANY, patch

ROOT = Path(__file__).resolve().parents[1]
if "catalystlab" not in sys.modules:
    package = types.ModuleType("catalystlab")
    package.__path__ = [str(ROOT / "src")] + [str(path) for path in (ROOT / "src").rglob("*") if path.is_dir()]
    sys.modules["catalystlab"] = package

from catalystlab.project_build_process import (
    ProjectBuild, CATALYST_WORK_PATH, CATALYST_BUILDS_PATH, CATALYST_KERNEL_CACHE_PATH,
    _move_stage_build_files,
)
from catalystlab.project_stage_arguments import StageArgumentDetails


class ProjectBuildPathTests(unittest.TestCase):
    def test_maps_each_generated_directory_and_its_children(self):
        process = ProjectBuild.__new__(ProjectBuild)
        process.work_root = "/library/Temporary/work"
        process.catalyst_builds_directory = "/library/Builds/Catalyst"
        process.kernel_cache_directory = "/library/Builds/kerncache"
        process.mirrored_paths = lambda: []

        self.assertEqual(process.container_path(process.work_root), CATALYST_WORK_PATH)
        self.assertEqual(process.container_path(process.work_root + "/stage.spec"),
                         CATALYST_WORK_PATH + "/stage.spec")
        self.assertEqual(process.container_path(process.catalyst_builds_directory), CATALYST_BUILDS_PATH)
        self.assertEqual(process.container_path(process.kernel_cache_directory + "/source"),
                         CATALYST_KERNEL_CACHE_PATH + "/source")

    def test_synced_caches_resolve_only_enabled_non_kernel_cache_once(self):
        project_id, packages_id = uuid.uuid4(), uuid.uuid4()
        packages = types.SimpleNamespace(id=packages_id, directory_path=lambda: f"/library/Packages/{packages_id.hex}")
        process = ProjectBuild.__new__(ProjectBuild)
        process.machine = True
        process.executor = types.SimpleNamespace(cache_directory=lambda: "/vm/cache")
        process.builds_directory = "/library/Builds"
        process.project_directory = types.SimpleNamespace(
            id=project_id, get_packages_directory=lambda: packages,
        )
        paths = {
            StageArgumentDetails.pkgcache_path: f"{packages.directory_path()}/23.0",
            StageArgumentDetails.kerncache_path: "/library/Builds/caches/kerncache/23.0",
        }
        with patch("catalystlab.project_build_process.stage_cache_path", side_effect=lambda _, __, key: paths[key]) as resolve, \
             patch("catalystlab.project_build_process.distfiles_directory", side_effect=["/library/Cache/Distfiles", "/vm/Distfiles"]):
            caches = process.synced_caches(object())

        self.assertEqual(caches, [
            ("/library/Cache/Distfiles", "/vm/Distfiles"),
            (paths[StageArgumentDetails.pkgcache_path], f"/vm/cache/Packages/{packages_id.hex}/23.0"),
        ])
        resolve.assert_called_once_with(process.project_directory, ANY, StageArgumentDetails.pkgcache_path)

    def test_moves_only_matching_stage_and_iso_outputs_and_sets_optional_owner(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            source, destination = Path(temporary, 'catalyst'), Path(temporary, 'build')
            source.mkdir()
            for name in ('stage4-arm64.tar.xz', 'stage4-arm64.DIGESTS', 'live.iso', 'live.iso.sha256', 'other.tar.xz'):
                (source / name).write_text(name)
            (source / 'stage4-arm64').mkdir()
            (source / 'stage4-arm64' / 'root').write_text('temporary catalyst output')

            with patch('catalystlab.project_build_process.os.chown') as chown:
                moved = _move_stage_build_files(str(source), 'stage4-arm64', str(destination), 'live.iso', owner_uid=501)
                self.assertEqual(moved, ['live.iso', 'live.iso.sha256', 'stage4-arm64.DIGESTS', 'stage4-arm64.tar.xz'])
                self.assertEqual(sorted(path.name for path in destination.iterdir()), moved)
                self.assertTrue((source / 'other.tar.xz').exists())
                self.assertFalse((source / 'stage4-arm64').exists())
                self.assertEqual(chown.call_count, 4)


if __name__ == "__main__":
    unittest.main()
