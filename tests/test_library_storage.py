"""Storage summaries and safe library cleanup paths."""
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.library_storage import remove_library_item, scan_library_storage


class LibraryStorageTests(unittest.TestCase):
    def test_scan_reports_library_sections_and_cache_temporary_items(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary, 'Library')
            cache = library / 'Cache'
            distfiles = cache / 'Distfiles'
            seeds = cache / 'catalyst-seeds'
            package_cache = cache / 'Packages' / 'packages-id' / '23.0-arm64'
            project_cache = cache / 'Projects' / 'project-id' / 'caches' / 'pkgcache' / '23.0-arm64'
            temp_files = library / 'Temporary' / 'VMLogs'
            distfiles.mkdir(parents=True)
            seeds.mkdir(parents=True)
            package_cache.mkdir(parents=True)
            project_cache.mkdir(parents=True)
            temp_files.mkdir(parents=True)
            (distfiles / 'source.tar.xz').write_bytes(b'cached source')
            (seeds / 'stage3.tar.xz').write_bytes(b'cached stage seed')
            (package_cache / 'package.gpkg.tar').write_bytes(b'binary package')
            (project_cache / 'package.gpkg.tar').write_bytes(b'project package cache')
            (temp_files / 'run.log').write_text('diagnostic log')
            (cache / '.partial-download').write_text('unknown cache item')
            (library / 'unrelated.txt').write_text('not a Catalyst Lab library section')

            result = scan_library_storage(str(library))

            self.assertGreater(result['total_size'], 0)
            self.assertEqual({section['name'] for section in result['sections']}, {'Cache', 'Temporary'})
            self.assertEqual({item['relative_path'] for item in result['cache_items']},
                             {'source.tar.xz', 'stage3.tar.xz', 'packages-id/23.0-arm64',
                              'project-id/caches/pkgcache/23.0-arm64'})
            cache_category_order = list(dict.fromkeys(item['category'] for item in result['cache_items']))
            self.assertEqual(cache_category_order[-1], 'Distfiles')
            seed_item = next(item for item in result['cache_items'] if item['name'] == 'stage3.tar.xz')
            self.assertEqual(seed_item['type_title'], 'Gentoo seed stages')
            self.assertIn('seed systems', seed_item['description'])
            self.assertIn('Modified', seed_item['details'])
            self.assertIn('packages-id/23.0-arm64', {
                item['relative_path'] for item in result['cache_items'] if item['category'] == 'Packages'
            })
            self.assertIn('project-id/caches/pkgcache/23.0-arm64', {
                item['relative_path'] for item in result['cache_items'] if item['category'] == 'Projects'
            })
            self.assertEqual(result['temporary_items'][0]['relative_path'], 'run.log')
            self.assertGreater(result['cache_items'][0]['size'], 0)
            self.assertGreater(result['temporary_items'][0]['size'], 0)

    def test_remove_deletes_selected_symlink_without_following_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary, 'Library')
            cache = library / 'Cache'
            distfiles = cache / 'Distfiles'
            distfiles.mkdir(parents=True)
            outside = Path(temporary, 'outside')
            outside.mkdir()
            kept_file = outside / 'keep.txt'
            kept_file.write_text('preserve')
            (distfiles / 'external.tar.xz').symlink_to(kept_file)

            remove_library_item(str(library), 'Cache', 'Distfiles', 'external.tar.xz')

            self.assertFalse((distfiles / 'external.tar.xz').is_symlink())
            self.assertEqual(kept_file.read_text(), 'preserve')

    def test_remove_rejects_paths_outside_cache_and_temporary(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary, 'Library')
            library.mkdir()
            for section, category, path in [
                ('Projects', 'project', 'project'),
                ('Cache', 'Distfiles', '../outside'),
                ('Cache', 'unknown', 'item'),
                ('Cache', 'Distfiles', 'one/../../outside'),
            ]:
                with self.subTest(section=section, category=category, path=path), self.assertRaises(ValueError):
                    remove_library_item(str(library), section, category, path)

    def test_remove_deletes_only_selected_nested_cache_object(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary, 'Library')
            selected = library / 'Cache' / 'Packages' / 'packages-id' / '23.0-arm64'
            sibling = library / 'Cache' / 'Packages' / 'packages-id' / '23.1-arm64'
            selected.mkdir(parents=True)
            sibling.mkdir(parents=True)
            (selected / 'one.gpkg.tar').write_text('selected')
            (sibling / 'two.gpkg.tar').write_text('keep')

            remove_library_item(str(library), 'Cache', 'Packages', 'packages-id/23.0-arm64')

            self.assertFalse(selected.exists())
            self.assertTrue((sibling / 'two.gpkg.tar').exists())

    def test_runner_workspace_is_listed_as_known_temporary_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary, 'Library')
            workspace = library / 'Temporary' / 'Runners' / 'catalystlab-12345678'
            workspace.mkdir(parents=True)
            (workspace / 'workspace.img').write_bytes(b'workspace')

            result = scan_library_storage(str(library))

            item = next(item for item in result['temporary_items'] if item['category'] == 'Runners')
            self.assertEqual(item['name'], 'catalystlab-12345678')
            self.assertEqual(item['type_title'], 'VM runner workspaces')
            remove_library_item(str(library), 'Temporary', 'Runners', item['relative_path'])
            self.assertFalse(workspace.exists())

    def test_scan_lists_registered_binpkgs_and_snapshots_with_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary, 'Library')
            packages = library / 'Packages' / 'Team packages'
            snapshot_root = library / 'Snapshots'
            packages.mkdir(parents=True)
            snapshot_root.mkdir(parents=True)
            (packages / 'metadata.json').write_text(
                '{"id":"packages-id","name":"Team packages","architecture":"arm64",'
                '"cpu_flags":"-mcpu=cortex-a76","created":"2026-10-18T12:00:00"}')
            (packages / '23.0-arm64').mkdir()
            (packages / '23.0-arm64' / 'pkg.gpkg.tar').write_bytes(b'package')
            snapshot = snapshot_root / ('gentoo-' + 'a' * 40 + '.sqfs')
            snapshot.write_bytes(b'squashfs-data')
            (snapshot.with_name(snapshot.name + '.json')).write_text('{"from_file":true}')
            (snapshot_root / '.ignored.sqfs').write_bytes(b'ignored')
            (library / 'Packages' / 'unregistered').mkdir()

            result = scan_library_storage(str(library))

            self.assertEqual([item['name'] for item in result['binpkg_items']], ['Team packages'])
            binpkg = result['binpkg_items'][0]
            self.assertIn('arm64', binpkg['details'])
            self.assertFalse(binpkg['in_use'])
            self.assertEqual(len(result['snapshot_items']), 1)
            self.assertIn('Imported snapshot', result['snapshot_items'][0]['details'])
            self.assertTrue(any('Gentoo commit' in value for value in result['snapshot_items'][0]['metadata']))

    def test_registered_binpkgs_and_snapshots_are_removed_individually(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary, 'Library')
            packages = library / 'Packages' / 'Team packages'
            packages.mkdir(parents=True)
            (packages / 'metadata.json').write_text('{"id":"packages-id","name":"Team packages"}')
            (packages / '23.0' ).mkdir()
            (packages / '23.0' / 'pkg.gpkg.tar').write_bytes(b'pkg')
            snapshots = library / 'Snapshots'
            snapshots.mkdir()
            snapshot = snapshots / ('gentoo-' + 'b' * 40 + '.sqfs')
            snapshot.write_bytes(b'snapshot')
            marker = Path(str(snapshot) + '.json')
            marker.write_text('{"from_file":true}')

            remove_library_item(str(library), 'Packages', 'registered', 'Team packages')
            remove_library_item(str(library), 'Snapshots', 'snapshot', snapshot.name)

            self.assertFalse(packages.exists())
            self.assertFalse(snapshot.exists())
            self.assertFalse(marker.exists())

    def test_project_references_are_informational_and_deletion_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary, 'Library')
            package_id = 'packages-id'
            packages = library / 'Packages' / 'Team packages'
            packages.mkdir(parents=True)
            (packages / 'metadata.json').write_text(
                '{"id":"packages-id","name":"Team packages","architecture":"arm64"}')
            projects = library / 'Projects' / 'Project one' / '.git'
            projects.mkdir(parents=True)
            snapshot_name = 'gentoo-' + 'c' * 40 + '.sqfs'
            (projects / 'catalystlab.json').write_text(
                '{"name":"Project one","metadata":{"packages_directory_id":"packages-id",'
                f'"snapshot_id":"{snapshot_name}"}}}}')
            (library / 'Snapshots').mkdir()
            (library / 'Snapshots' / snapshot_name).write_bytes(b'snapshot')

            result = scan_library_storage(str(library))
            self.assertTrue(result['binpkg_items'][0]['in_use'])
            self.assertTrue(result['snapshot_items'][0]['in_use'])
            remove_library_item(str(library), 'Packages', 'registered', 'Team packages')
            remove_library_item(str(library), 'Snapshots', 'snapshot', snapshot_name)
            self.assertFalse(packages.exists())
            self.assertFalse((library / 'Snapshots' / snapshot_name).exists())

    def test_latest_snapshot_projects_are_not_reported_as_snapshot_references(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary, 'Library')
            snapshots = library / 'Snapshots'
            snapshots.mkdir(parents=True)
            snapshot_name = 'gentoo-' + 'd' * 40 + '.sqfs'
            (snapshots / snapshot_name).write_bytes(b'snapshot')

            projects = library / 'Projects'
            pinned = projects / 'Pinned project' / '.git'
            latest = projects / 'Latest project' / '.git'
            pinned.mkdir(parents=True)
            latest.mkdir(parents=True)
            (pinned / 'catalystlab.json').write_text(
                '{"name":"Pinned project","metadata":{"snapshot_id":"' + snapshot_name + '"}}')
            (latest / 'catalystlab.json').write_text(
                '{"name":"Latest project","metadata":{"latest_snapshot":true,'
                '"snapshot_id":"' + snapshot_name + '"}}')

            result = scan_library_storage(str(library))

            self.assertEqual(result['snapshot_items'][0]['projects'], ['Pinned project'])
            self.assertIn('Pinned project', result['snapshot_items'][0]['usage'])
            self.assertNotIn('Latest project', result['snapshot_items'][0]['usage'])

    def test_scan_lists_only_registered_projects_and_known_project_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary, 'Library')
            project = library / 'Projects' / 'Raspberry Pi 5'
            git_metadata = project / '.git' / 'catalystlab.json'
            git_metadata.parent.mkdir(parents=True)
            git_metadata.write_text(
                '{"name":"Raspberry Pi 5","id":"11111111-2222-3333-4444-555555555555",'
                '"metadata":{"architecture":"arm64","latest_snapshot":true,'
                '"packages_directory_id":"packages-id"}}')
            (project / 'stages' / 'stage4-systemd').mkdir(parents=True)
            build = (library / 'Builds' / '11111111222233334444555555555555' / 'stages' /
                     'stage-id' / '2026-10-18T12:00:00')
            build.mkdir(parents=True)
            (build / 'build.json').write_text('{}')
            (library / 'Projects' / 'unregistered').mkdir()

            result = scan_library_storage(str(library))

            self.assertEqual(len(result['project_items']), 1)
            item = result['project_items'][0]
            self.assertEqual(item['name'], 'Raspberry Pi 5')
            self.assertIn('1 stage(s)', item['details'])
            self.assertIn('1 build(s)', item['details'])
            self.assertTrue(any('Latest snapshot' in value for value in item['metadata']))

    def test_builds_tab_data_lists_and_deletes_only_recognized_builds(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary, 'Library')
            builds_root = library / 'Builds' / 'project-id' / 'stages'
            selected = builds_root / 'stage-id' / '2026-10-18T12:00:00'
            sibling = builds_root / 'stage-id' / '2026-10-17T12:00:00'
            selected.mkdir(parents=True)
            sibling.mkdir(parents=True)
            (selected / 'build.json').write_text(
                '{"id":"build-id","stage_name":"stage4-systemd","status":"completed",'
                '"timestamp":"2026-10-18T12:00:00","artifact":"stage.tar.xz"}')
            (selected / 'stage.tar.xz').write_bytes(b'artifact')
            (sibling / 'build.json').write_text(
                '{"id":"sibling-id","stage_name":"stage4-systemd","status":"failed",'
                '"timestamp":"2026-10-17T12:00:00"}')
            (builds_root / 'stage-id' / 'not-a-build').mkdir()

            result = scan_library_storage(str(library))

            self.assertEqual(len(result['build_items']), 2)
            item = next(item for item in result['build_items'] if item['timestamp'] == '2026-10-18T12:00:00')
            self.assertEqual(item['name'], 'stage4-systemd')
            self.assertTrue(any('Artifact: stage.tar.xz' in value for value in item['metadata']))
            remove_library_item(str(library), 'Builds', 'build', item['relative_path'])

            self.assertFalse(selected.exists())
            self.assertTrue(sibling.exists())


if __name__ == '__main__':
    unittest.main()
