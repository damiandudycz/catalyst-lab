"""Partial Git directory cleanup is limited to failed creation steps."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from gi.repository import Gio

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.git_installation import (
    GitDirectorySource, GitInstallation, GitInstallationStepInitLocal,
    GitInstallationStepSetupRepository, _GitInstallationDirectoryStep,
    _copy_local_directory, _remove_partial_directory,
)
from catalystlab.git_update import GitUpdate
from catalystlab.multistage_process import MultiStageProcessStageState, MultiStageProcessState


class GitInstallationTests(unittest.TestCase):
    def test_git_processes_resolve_their_manager_from_the_subclass(self):
        manager = object()

        class Manager:
            @classmethod
            def shared(cls):
                return manager

        class Installation(GitInstallation):
            manager_type = Manager

        class Update(GitUpdate):
            manager_type = Manager

        self.assertIs(Installation.manager(), manager)
        self.assertIs(Update.manager(), manager)

    def test_source_enum_keeps_standard_name_and_ui_label(self):
        source = GitDirectorySource.GIT_REPOSITORY

        self.assertEqual(source.name, 'GIT_REPOSITORY')
        self.assertEqual(source.display_name(), 'Git repository')

    def test_repository_setup_uses_directory_runner_for_initial_commit(self):
        directory = SimpleNamespace(
            directory_path=lambda: '/project',
        )
        directory._run_git = Mock(side_effect=[
            SimpleNamespace(returncode=128, stdout=''),
            SimpleNamespace(returncode=0, stdout='?? README.md\n'),
        ])
        directory.run_git_logged = Mock(return_value=SimpleNamespace(
            returncode=0, args=['git'], stdout=''
        ))
        step = GitInstallationStepSetupRepository(SimpleNamespace(directory=directory))

        with patch.object(step, 'complete') as complete:
            step.start()

        self.assertEqual([call.args[0] for call in directory.run_git_logged.call_args_list], [
            ('init', '--initial-branch=main'), ('add', '.'), ('commit', '-m', 'Initial commit')
        ])
        self.assertEqual(complete.call_args.args[0], MultiStageProcessStageState.COMPLETED)

    def test_directory_creation_shared_lifecycle_sets_process_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            created = Path(temporary, 'project')

            class Directory:
                def __init__(self, name):
                    self.name = name

                def directory_path(self):
                    return str(created)

            process = SimpleNamespace(directory=None)
            step = GitInstallationStepInitLocal('project', None, Directory, process)
            with patch.object(step, 'complete') as complete:
                step.start()

            self.assertTrue(created.is_dir())
            self.assertIs(process.directory, step.directory)
            complete.assert_called_once_with(MultiStageProcessStageState.COMPLETED)

    def test_directory_creation_failure_uses_shared_failure_transition(self):
        with tempfile.TemporaryDirectory() as temporary:
            created = Path(temporary, 'project')
            created.mkdir()

            class Directory:
                def __init__(self, name):
                    self.name = name

                def directory_path(self):
                    return str(created)

            process = SimpleNamespace(directory=None)
            step = GitInstallationStepInitLocal('project', None, Directory, process)
            with patch.object(step, 'complete') as complete:
                step.start()

            self.assertIsNone(process.directory)
            complete.assert_called_once_with(MultiStageProcessStageState.FAILED)

    def test_local_directory_copy_preserves_tree_and_reports_file_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_path = Path(temporary, 'source')
            (source_path / 'nested').mkdir(parents=True)
            (source_path / 'empty').mkdir()
            (source_path / 'first.txt').write_text('one')
            (source_path / 'nested' / 'second.txt').write_text('two')
            destination_path = Path(temporary, 'destination')
            destination_path.mkdir()
            progress = []

            _copy_local_directory(
                Gio.File.new_for_path(str(source_path)),
                Gio.File.new_for_path(str(destination_path)),
                progress.append,
            )

            self.assertEqual((destination_path / 'first.txt').read_text(), 'one')
            self.assertEqual((destination_path / 'nested' / 'second.txt').read_text(), 'two')
            self.assertTrue((destination_path / 'empty').is_dir())
            self.assertEqual(progress, [0.5, 1.0])

    def test_directory_steps_share_failed_creation_cleanup(self):
        class Step(_GitInstallationDirectoryStep):
            def start(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, 'project')
            path.mkdir()
            process = SimpleNamespace(status=MultiStageProcessState.FAILED)
            step = Step('clone', 'clone repo', process)
            step.directory = SimpleNamespace(directory_path=lambda: str(path))
            step.process_started = True
            step.state = MultiStageProcessStageState.FAILED

            self.assertTrue(step.cleanup())
            self.assertFalse(path.exists())

    def test_cleanup_removes_only_started_failed_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, 'project')
            path.mkdir()
            step = SimpleNamespace(
                multistage_process=SimpleNamespace(status=MultiStageProcessState.FAILED),
                process_started=True,
                directory=SimpleNamespace(directory_path=lambda: str(path)),
            )
            _remove_partial_directory(step)
            self.assertFalse(path.exists())

            path.mkdir()
            step.multistage_process.status = MultiStageProcessState.COMPLETED
            _remove_partial_directory(step)
            self.assertTrue(path.is_dir())


if __name__ == '__main__':
    unittest.main()
