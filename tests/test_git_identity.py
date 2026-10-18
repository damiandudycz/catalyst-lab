"""System and app-specific Git author identity behavior."""
from pathlib import Path
import sys
import os
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT)] + [str(path) for path in ROOT.rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.git_identity import (
    GitIdentity, effective_git_identity, git_environment, require_git_identity, system_git_identity,
)
from catalystlab.settings import Settings


def fake_repositories(settings):
    module = types.ModuleType('catalystlab.repositories')
    module.Repository = types.SimpleNamespace(Settings=types.SimpleNamespace(value=settings))
    return module


class GitIdentityTests(unittest.TestCase):
    def test_reads_system_git_name_and_email(self):
        outputs = [types.SimpleNamespace(returncode=0, stdout='System User\n'),
                   types.SimpleNamespace(returncode=0, stdout='system@example.com\n')]
        with patch('catalystlab.git_identity.subprocess.run', side_effect=outputs):
            self.assertEqual(system_git_identity(), GitIdentity('System User', 'system@example.com'))

    def test_system_mode_uses_host_config_and_custom_mode_uses_app_settings(self):
        settings = Settings(git_identity_mode='custom', git_user_name='App User',
                            git_user_email='app@example.com')
        with patch.dict(sys.modules, {'catalystlab.repositories': fake_repositories(settings)}):
            self.assertEqual(effective_git_identity(), GitIdentity('App User', 'app@example.com'))
            environment = git_environment({'PATH': '/bin'})
        self.assertEqual(environment['GIT_AUTHOR_NAME'], 'App User')
        self.assertEqual(environment['GIT_AUTHOR_EMAIL'], 'app@example.com')
        self.assertEqual(environment['GIT_COMMITTER_NAME'], 'App User')
        self.assertEqual(environment['PATH'], '/bin')

    def test_missing_system_identity_has_actionable_error(self):
        settings = Settings()
        with patch.dict(sys.modules, {'catalystlab.repositories': fake_repositories(settings)}), \
             patch('catalystlab.git_identity.system_git_identity', return_value=GitIdentity()):
            with self.assertRaisesRegex(RuntimeError, 'Settings under Git identity'):
                require_git_identity()

    def test_custom_identity_is_enough_to_create_a_commit_without_git_config(self):
        settings = Settings(git_identity_mode='custom', git_user_name='App User',
                            git_user_email='app@example.com')
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(sys.modules, {'catalystlab.repositories': fake_repositories(settings)}):
            subprocess.run(['git', 'init', '--quiet', directory], check=True)
            with open(os.path.join(directory, 'file.txt'), 'w', encoding='utf-8') as file:
                file.write('content')
            subprocess.run(['git', '-C', directory, 'add', '.'], check=True)
            require_git_identity(directory)
            subprocess.run(['git', '-C', directory, 'commit', '--quiet', '-m', 'Initial'],
                           check=True, env=git_environment())
            author = subprocess.run(['git', '-C', directory, 'log', '-1', '--format=%an <%ae>'],
                                    check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(author, 'App User <app@example.com>')


if __name__ == '__main__':
    unittest.main()
