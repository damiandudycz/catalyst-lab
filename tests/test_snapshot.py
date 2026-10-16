"""Snapshot metadata, archive parsing, and Gentoo commit lookup behavior."""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import snapshot as snapshot_module
from catalystlab.snapshot import Snapshot, PortageProfile, parse_profiles_desc
from catalystlab.architecture import Architecture


class SnapshotMetadataTests(unittest.TestCase):
    def test_load_all_sorts_snapshots_and_reads_import_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'old.sqfs').write_bytes(b'old')
            (root/'imported.sqfs').write_bytes(b'imported')
            (root/'ignored.txt').write_text('not a snapshot')
            (root/'folder.sqfs').mkdir()
            (root/'imported.sqfs.json').write_text('{"from_file": true}')
            os_old = 1_700_000_000
            os_new = 1_700_000_100
            os.utime(root/'old.sqfs', (os_old, os_old))
            os.utime(root/'imported.sqfs', (os_new, os_new))
            with patch.object(Snapshot, 'base_location', return_value=temporary):
                items = Snapshot.load_all()
        self.assertEqual([item.filename for item in items], ['imported.sqfs', 'old.sqfs'])
        self.assertTrue(items[0].from_file)
        self.assertFalse(items[1].from_file)

    def test_load_all_handles_missing_directory_and_invalid_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Snapshot, 'base_location', return_value=str(Path(temporary)/'missing')):
                self.assertEqual(Snapshot.load_all(), [])
            (Path(temporary)/'broken.sqfs').touch()
            (Path(temporary)/'broken.sqfs.json').write_text('{')
            with patch.object(Snapshot, 'base_location', return_value=temporary), patch('builtins.print'):
                items = Snapshot.load_all()
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0].from_file)

    def test_save_all_persists_import_marker_and_removes_stale_generated_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            imported = Snapshot('imported.sqfs', datetime(2026, 1, 1), True)
            generated = Snapshot('gentoo-' + 'a'*40 + '.sqfs', datetime(2026, 1, 2))
            marker = Path(temporary)/generated.filename
            Path(temporary, imported.filename).touch()
            Path(temporary, generated.filename).touch()
            Path(str(marker)+'.json').write_text('{"from_file": true}')
            with patch.object(Snapshot, 'base_location', return_value=temporary):
                Snapshot.save_all([imported, generated])
            self.assertEqual(json.loads(Path(temporary, imported.filename+'.json').read_text()), {'from_file': True})
            self.assertFalse(Path(str(marker)+'.json').exists())

    def test_snapshot_serialization_and_display_properties(self):
        date = datetime(2026, 10, 16, 12, 34)
        snapshot = Snapshot('custom.sqfs', date, True)
        restored = Snapshot.init_from(snapshot.serialize())
        self.assertEqual(restored, snapshot)
        self.assertEqual(snapshot.name, '2026-10-16 12:34')
        self.assertEqual(snapshot.short_details, 'custom')
        self.assertTrue(snapshot.may_contain_own_changes)
        self.assertEqual(Snapshot('old.sqfs', None).name, '(Unknown date)')
        self.assertFalse(Snapshot('gentoo-'+'f'*40+'.sqfs', date).may_contain_own_changes)

    def test_load_ebuilds_groups_and_sorts_valid_package_versions(self):
        output = '\n'.join([
            'squashfs-root/dev-libs/zlib/zlib-1.2.13.ebuild',
            'squashfs-root/dev-libs/zlib/zlib-1.3.ebuild',
            'squashfs-root/app-editors/vim/vim-9.0.ebuild',
            'squashfs-root/dev-libs/zlib/not-zlib.ebuild',
            'squashfs-root/malformed.ebuild',
            'other/dev-libs/pkg/pkg-1.ebuild',
        ])
        snapshot = Snapshot('repo.sqfs', None)
        with patch.object(Snapshot, 'base_location', return_value='/snapshots'), patch.object(snapshot_module.subprocess, 'check_output', return_value=output) as run:
            self.assertEqual(snapshot.load_ebuilds(), {
                'app-editors': {'vim': ['9.0']},
                'dev-libs': {'zlib': ['1.2.13', '1.3']},
            })
        run.assert_called_once_with(['unsquashfs', '-l', '/snapshots/repo.sqfs'], text=True)

    def test_load_ebuilds_returns_empty_on_squashfs_failure(self):
        import subprocess
        with patch.object(Snapshot, 'base_location', return_value='/snapshots'), patch.object(snapshot_module.subprocess, 'check_output', side_effect=subprocess.CalledProcessError(1, 'unsquashfs')), patch('builtins.print'):
            self.assertEqual(Snapshot('bad.sqfs', None).load_ebuilds(), {})

    def test_profiles_filter_architecture_and_ignore_comments_or_malformed_lines(self):
        contents = '\n'.join([
            '# comment',
            'arm64 default/linux/arm64/23.0 stable',
            'amd64 default/linux/amd64/23.0 stable',
            'arm64 incomplete',
            '',
        ])
        self.assertEqual(parse_profiles_desc(contents, Architecture.arm64, 'gentoo'), [
            PortageProfile('default/linux/arm64/23.0', 'stable', 'gentoo'),
        ])

    def test_load_profiles_reads_archive_profile_description(self):
        snapshot = Snapshot('repo.sqfs', None)
        with patch.object(Snapshot, 'base_location', return_value='/snapshots'), patch.object(snapshot_module.subprocess, 'check_output', return_value='arm64 default/linux/arm64 stable\n') as run:
            profiles = snapshot.load_profiles(Architecture.arm64)
        run.assert_called_once_with(['unsquashfs', '-cat', '/snapshots/repo.sqfs', 'profiles/profiles.desc'], text=True)
        self.assertEqual(profiles[0].path, 'default/linux/arm64')

    def test_portage_profile_serialization_and_identity(self):
        profile = PortageProfile('default/linux/arm64', 'stable', 'gentoo')
        self.assertEqual(PortageProfile.init_from(profile.serialize()), profile)
        self.assertEqual(len({profile, PortageProfile('default/linux/arm64', 'dev', 'gentoo')}), 1)
        self.assertNotEqual(profile, PortageProfile('default/linux/arm64', 'stable', 'custom'))


class LatestSnapshotTests(unittest.TestCase):
    def test_latest_commit_accepts_only_full_lowercase_hash(self):
        good = 'a' * 40
        with patch.object(snapshot_module.subprocess, 'run', return_value=types.SimpleNamespace(stdout=f'{good}\trefs/heads/stable\n')) as run:
            self.assertEqual(snapshot_module.latest_gentoo_commit(), good)
        self.assertEqual(run.call_args.kwargs['timeout'], 30)
        for output in ('', 'short refs/heads/stable\n', 'A'*40+' refs/heads/stable'):
            with self.subTest(output=output), patch.object(snapshot_module.subprocess, 'run', return_value=types.SimpleNamespace(stdout=output)):
                self.assertIsNone(snapshot_module.latest_gentoo_commit())

    def test_latest_commit_handles_command_failure(self):
        with patch.object(snapshot_module.subprocess, 'run', side_effect=OSError('offline')), patch('builtins.print'):
            self.assertIsNone(snapshot_module.latest_gentoo_commit())

    def test_existing_latest_snapshot_requires_local_generated_file(self):
        commit = 'a' * 40
        generated = Snapshot(f'gentoo-{commit}.sqfs', datetime.now())
        imported = Snapshot(f'gentoo-{commit}.sqfs', datetime.now(), from_file=True)
        repo = types.SimpleNamespace(Snapshot=types.SimpleNamespace(value=[imported, generated]))
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(snapshot_module, 'Repository', repo), patch.object(snapshot_module, 'latest_gentoo_commit', return_value=commit), patch.object(generated, 'file_path', return_value=str(Path(temporary)/generated.filename)):
                self.assertIsNone(snapshot_module.existing_latest_snapshot(log=lambda _: None))
                Path(generated.file_path()).touch()
                self.assertIs(snapshot_module.existing_latest_snapshot(log=lambda _: None), generated)

    def test_existing_latest_snapshot_logs_when_remote_commit_unavailable(self):
        messages = []
        with patch.object(snapshot_module, 'latest_gentoo_commit', return_value=None):
            self.assertIsNone(snapshot_module.existing_latest_snapshot(messages.append))
        self.assertIn('snapshot will be generated', messages[0])


if __name__ == '__main__':
    unittest.main()
