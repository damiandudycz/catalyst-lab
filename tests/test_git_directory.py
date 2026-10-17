"""Git directory status and command execution."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import git_directory as module, git_manager
from catalystlab.git_directory import GitDirectory, GitDirectoryStatus


class TestDirectory(GitDirectory):
    @classmethod
    def base_location(cls):
        return ''

    def __init__(self, path):
        super().__init__('test')
        self.path = str(path)

    def directory_path(self):
        return self.path


class GitDirectoryTests(unittest.TestCase):
    def test_logged_git_command_shares_command_and_output_logging(self):
        directory = TestDirectory('/tmp/repository')
        result = types.SimpleNamespace(returncode=1, stdout='remote unavailable\n')
        log = []
        environment = {'GIT_TERMINAL_PROMPT': '0'}

        with patch.object(directory, '_run_git', return_value=result) as run:
            returned = directory.run_git_logged(['fetch', '--all'], log.append, env=environment)

        self.assertIs(returned, result)
        self.assertEqual(log, ['$ git fetch --all', 'remote unavailable'])
        run.assert_called_once_with('fetch', '--all', stdout=module.subprocess.PIPE,
                                    stderr=module.subprocess.STDOUT, text=True, env=environment)

    def test_worktree_git_file_resolves_metadata_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / 'project'
            worktree.mkdir()
            git_metadata = root / 'repository.git' / 'worktrees' / 'project'
            git_metadata.mkdir(parents=True)
            (worktree / '.git').write_text('gitdir: ../repository.git/worktrees/project\n')

            self.assertTrue(GitDirectory.is_git_directory(str(worktree)))
            self.assertEqual(
                GitDirectory._app_metadata_path(str(worktree)),
                str(git_metadata / GitDirectory.APP_METADATA_FILE),
            )

    def test_manager_add_replaces_matching_id_with_one_repository_update(self):
        class FakeRepository:
            def __init__(self, items):
                self._value = items
                self.assignments = 0

            @property
            def value(self):
                return self._value

            @value.setter
            def value(self, items):
                self.assignments += 1
                self._value = items

        class Manager(git_manager.GitManager):
            current_repository = None

            @classmethod
            def repository(cls):
                return cls.current_repository

        replaced = types.SimpleNamespace(id='replace')
        retained = types.SimpleNamespace(id='keep')
        replacement = types.SimpleNamespace(id='replace')
        repository = FakeRepository([replaced, retained])
        Manager.current_repository = repository

        Manager().add_directory(replacement)

        self.assertEqual(repository.value, [retained, replacement])
        self.assertEqual(repository.assignments, 1)

    def test_status_reads_git_metadata_through_repository_scoped_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, '.git').mkdir()
            directory = TestDirectory(temporary)
            results = [
                types.SimpleNamespace(returncode=0, stdout='UU conflict.txt\n'),
                types.SimpleNamespace(returncode=0, stdout='2026-10-17T12:00:00+02:00\n'),
                types.SimpleNamespace(returncode=0, stdout='main\n'),
                types.SimpleNamespace(returncode=0, stdout='https://example.test/repo.git\n'),
                types.SimpleNamespace(returncode=0, stdout=''),
                types.SimpleNamespace(returncode=0, stdout='3\n'),
            ]
            with patch.object(module.subprocess, 'run', side_effect=results) as run:
                directory.update_status(wait=True)

            self.assertEqual(directory.status, GitDirectoryStatus.CONFLICTED)
            self.assertEqual(directory.branch_name, 'main')
            self.assertEqual(directory.remote_url, 'https://example.test/repo.git')
            self.assertEqual(directory.last_commit_date.isoformat(), '2026-10-17T12:00:00+02:00')
            self.assertTrue(directory.has_remote_changes)
            self.assertEqual(run.call_count, 6)
            self.assertTrue(all(call.kwargs['cwd'] == temporary for call in run.call_args_list))

    def test_commit_with_no_staged_changes_still_refreshes_directory(self):
        directory = TestDirectory('/tmp/repository')
        run_change_inline = lambda worker, wait: worker()
        with patch.object(module, '_run_worker', side_effect=run_change_inline), \
             patch.object(directory, '_run_git', side_effect=[None, types.SimpleNamespace(returncode=0)]) as run, \
             patch.object(directory, 'update_status') as update_status, \
             patch.object(directory, 'update_logs') as update_logs:
            directory.commit_changes(wait=True)

        self.assertEqual([call.args for call in run.call_args_list], [
            ('add', '-A'), ('diff', '--cached', '--quiet')
        ])
        update_status.assert_called_once_with(wait=True)
        update_logs.assert_called_once_with(wait=True)
        self.assertFalse(directory.is_busy)

    def test_discard_failure_still_clears_busy_state_and_refreshes(self):
        directory = TestDirectory('/tmp/repository')
        run_change_inline = lambda worker, wait: worker()
        with patch.object(module, '_run_worker', side_effect=run_change_inline), \
             patch.object(directory, '_run_git', side_effect=RuntimeError('reset failed')) as run, \
             patch.object(directory, 'update_status') as update_status, \
             patch.object(directory, 'update_logs') as update_logs:
            directory.discard_changes(wait=True)

        run.assert_called_once_with('reset', '--hard', check=True)
        update_status.assert_called_once_with(wait=True)
        update_logs.assert_called_once_with(wait=True)
        self.assertFalse(directory.is_busy)


if __name__ == '__main__':
    unittest.main()
