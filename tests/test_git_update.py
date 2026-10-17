"""Git directory updates abort failed rebases in one place."""
from contextlib import nullcontext
from pathlib import Path
import sys
import types
import unittest
import tempfile
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import git_update
from catalystlab.git_directory import GitDirectory


class GitUpdateTests(unittest.TestCase):
    def test_git_directory_accepts_worktree_marker_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '.git').write_text('gitdir: /somewhere/else/.git/worktrees/project')
            self.assertTrue(GitDirectory.is_git_directory(str(root)))

    def test_git_directory_rejects_missing_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertFalse(GitDirectory.is_git_directory(temporary))

    def test_failed_rebase_is_aborted_once(self):
        directory = Mock(directory_path=Mock(return_value='/project'), operation=Mock(return_value=nullcontext()))
        directory.update_status = Mock()
        directory.update_logs = Mock()
        results = [
            types.SimpleNamespace(returncode=0, stdout='fetched'),
            types.SimpleNamespace(returncode=1, stdout='conflict'),
        ]
        with patch.object(git_update.os.path, 'isdir', return_value=True), \
             patch.object(directory, 'run_git_logged', side_effect=results) as logged, \
             patch.object(git_update.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'git rebase failed'):
                git_update.update_git_directory(directory, Mock())

        self.assertEqual([call.args[0] for call in logged.call_args_list], [
            ('fetch', '--all', '--prune'), ('rebase', '--autostash', '--rebase-merges', 'origin/HEAD')
        ])
        run.assert_called_once_with(['git', 'rebase', '--abort'], cwd='/project', capture_output=True)
        directory.update_status.assert_called_once_with(wait=True)
        directory.update_logs.assert_called_once_with(wait=True)


if __name__ == '__main__':
    unittest.main()
