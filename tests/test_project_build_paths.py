"""Project build paths are translated consistently into the toolset."""
from pathlib import Path
import json
import tempfile
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
    ProjectBuild, EmergeProgress, CATALYST_WORK_PATH, CATALYST_BUILDS_PATH, CATALYST_KERNEL_CACHE_PATH,
    _move_stage_build_files,
)
from catalystlab.project_build import StageBuild, delete_project_builds
from catalystlab.project_build import load_project_builds
from catalystlab.project_stage_arguments import StageArgumentDetails


class ProjectBuildPathTests(unittest.TestCase):
    def test_malformed_build_metadata_is_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage_path = Path(temporary, 'stages', 'stage-id', 'run')
            stage_path.mkdir(parents=True)
            (stage_path / StageBuild.METADATA_FILE).write_text(json.dumps({
                "id": str(uuid.uuid4()), "stage_id": str(uuid.uuid4()), "stage_name": "stage4",
                "timestamp": "run", "status": "in_progress", "date": 123,
            }))
            with patch('catalystlab.project_build.project_builds_directory', return_value=temporary):
                self.assertEqual(load_project_builds(object()), [])

    def test_emerge_progress_skips_seed_update_and_single_package_emerges(self):
        progress = EmergeProgress(completed_only=True, ignore_seed_update=True)
        self.assertIsNone(progress.parse(">>> Completed (1 of 1) sys-apps/baselayout"))
        self.assertIsNone(progress.parse("Updating seed stage"))
        self.assertIsNone(progress.parse("emerge --update @world"))
        self.assertIsNone(progress.parse(">>> Completed (8 of 10) seed package"))
        self.assertIsNone(progress.parse("emerge --newuse @world"))
        self.assertAlmostEqual(progress.parse("\x1b[32m>>> Completed (3 of 10) package\x1b[0m"), 0.3)

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

    def test_build_deletion_only_accepts_descendants_of_builds_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            builds_root = Path(temporary, "Builds")
            builds_root.mkdir()
            outside = Path(temporary, "Builds-copy")
            outside.mkdir()
            unsafe_build = StageBuild(uuid.uuid4(), "stage", "run", path=str(outside))

            with patch("catalystlab.project_build.builds_location", return_value=str(builds_root)):
                with self.assertRaises(RuntimeError):
                    unsafe_build.delete()
            self.assertTrue(outside.exists())

    def test_project_build_deletion_allows_a_configured_root_symlink_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            real_root = Path(temporary, "real-builds")
            real_root.mkdir()
            configured_root = Path(temporary, "configured-builds")
            configured_root.symlink_to(real_root, target_is_directory=True)
            project_id = uuid.uuid4()
            project_path = real_root / project_id.hex
            project_path.mkdir()
            (project_path / "build.log").write_text("build output")
            project = types.SimpleNamespace(id=project_id)

            with patch("catalystlab.project_build.builds_location", return_value=str(configured_root)):
                delete_project_builds(project)

            self.assertFalse(project_path.exists())


if __name__ == "__main__":
    unittest.main()
