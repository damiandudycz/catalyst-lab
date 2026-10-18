"""Snapshot retention depends on current configuration, not build history."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(p) for p in (ROOT / 'src').rglob('*') if p.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import snapshot_manager as manager
from catalystlab.multistage_process import MultiStageProcess, MultiStageProcessState


class SnapshotCleanupTests(unittest.TestCase):
    def setUp(self):
        now = datetime.now()
        self.old = manager.Snapshot('gentoo-' + 'a' * 40 + '.sqfs', now - timedelta(days=2))
        self.selected = manager.Snapshot('gentoo-' + 'b' * 40 + '.sqfs', now - timedelta(days=1))
        self.latest = manager.Snapshot('gentoo-' + 'c' * 40 + '.sqfs', now)
        self.imported = manager.Snapshot('custom.sqfs', now - timedelta(days=3), from_file=True)
        self.project = types.SimpleNamespace(get_snapshot=lambda: self.selected,
            builds=[types.SimpleNamespace(details={'inputs': {'snapshot': {'file': self.old.filename}}})])
        self.repository = types.SimpleNamespace(
            Snapshot=types.SimpleNamespace(value=[self.old, self.latest, self.selected, self.imported]),
            ProjectDirectory=types.SimpleNamespace(value=[self.project]),
            Settings=types.SimpleNamespace(value=types.SimpleNamespace(auto_clean_snapshots=True)))
        self.repository_patch = patch.object(manager, 'Repository', self.repository)
        self.repository_patch.start()
        self.addCleanup(self.repository_patch.stop)
        self.process_patch = patch.object(MultiStageProcess, 'started_processes', [])
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)
        self.manager = manager.SnapshotManager()

    def test_previous_build_and_import_do_not_retain_old_snapshots(self):
        self.assertEqual(self.manager.unused_snapshots(), [self.old, self.imported])

    def test_refresh_reloads_snapshots_from_existing_library_folder(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(manager.Snapshot, 'base_location', return_value=directory), \
                patch.object(manager.Snapshot, 'load_all', return_value=[self.latest]) as load_all:
            self.manager.refresh()
        load_all.assert_called_once_with()
        self.assertEqual(self.repository.Snapshot.value, [self.latest])

    def test_refresh_clears_stale_snapshots_when_library_folder_is_missing(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(manager.Snapshot, 'base_location', return_value=str(Path(directory, 'missing'))):
            self.manager.refresh()

        self.assertEqual(self.repository.Snapshot.value, [])

    def test_running_operation_protects_snapshot_only_until_finished(self):
        process = types.SimpleNamespace(snapshot=self.old, status=MultiStageProcessState.IN_PROGRESS)
        MultiStageProcess.started_processes.append(process)
        self.assertEqual(self.manager.unused_snapshots(), [self.imported])
        process.status = None
        self.assertEqual(self.manager.unused_snapshots(), [self.old, self.imported])

    def test_cleanup_removes_only_eligible_snapshots_when_enabled(self):
        with patch.object(self.manager, 'remove_snapshot') as remove:
            self.manager.auto_clean()
            self.assertEqual([call.args[0] for call in remove.call_args_list], [self.old, self.imported])
            remove.reset_mock()
            self.repository.Settings.value.auto_clean_snapshots = False
            self.manager.auto_clean()
            remove.assert_not_called()

    def test_empty_repository(self):
        self.repository.Snapshot.value = []
        self.assertEqual(self.manager.unused_snapshots(), [])

    def test_remove_snapshot_clears_archive_marker_cache_and_repository_entry(self):
        from catalystlab import rootless
        snapshot = self.old
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary, snapshot.filename)
            marker = Path(str(archive) + '.json')
            archive.touch()
            marker.write_text('{"from_file": true}')
            self.repository.Snapshot.value = [snapshot]
            with patch.object(manager.Snapshot, 'base_location', return_value=temporary), \
                 patch.object(rootless, 'remove_extracted_squashfs') as remove_cache, \
                 patch.object(manager.Repository, 'VMRunner', types.SimpleNamespace(value=[]), create=True):
                self.manager.remove_snapshot(snapshot)
            remove_cache.assert_called_once_with(str(archive), kind='snapshots')
            self.assertFalse(archive.exists())
            self.assertFalse(marker.exists())
            self.assertEqual(self.repository.Snapshot.value, [])

    def test_remove_snapshot_does_not_visit_machines_for_temporary_extractions(self):
        from catalystlab import rootless
        machine = types.SimpleNamespace(is_running=True)
        self.repository.VMRunner = types.SimpleNamespace(value=[machine])
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary, self.old.filename)
            archive.touch()
            self.repository.Snapshot.value = [self.old]
            with patch.object(manager.Snapshot, 'base_location', return_value=temporary), \
                 patch.object(rootless, 'remove_extracted_squashfs') as remove_cache, \
                 patch.object(rootless, 'MachineExecutor', side_effect=AssertionError('snapshot cache is temporary')) as executor, \
                 patch.object(manager.Repository, 'VMRunner', self.repository.VMRunner, create=True):
                self.manager.remove_snapshot(self.old)
            executor.assert_not_called()
            remove_cache.assert_called_once_with(str(archive), kind='snapshots')
