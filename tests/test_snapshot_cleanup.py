"""Snapshot retention depends on current configuration, not build history."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
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
