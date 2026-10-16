"""Host account suggestions use login identity and only the name part of GECOS."""
import os
import pwd
import types
import unittest
from unittest.mock import patch
import test_local_wifi
from catalystlab.deploy_system import local_user_identity


class LocalUserIdentityTests(unittest.TestCase):
    def test_host_username_and_full_name(self):
        with patch.object(pwd, 'getpwuid', return_value=types.SimpleNamespace(pw_name='damian', pw_gecos='Damian Dudycz,office,phone')) as lookup:
            self.assertEqual(local_user_identity(), ('damian', 'Damian Dudycz'))
            lookup.assert_called_once_with(os.getuid())

    def test_unavailable_account(self):
        with patch.object(pwd, 'getpwuid', side_effect=KeyError()):
            self.assertEqual(local_user_identity(), ('', ''))

    def test_invalid_or_root_login_is_not_suggested(self):
        for username in ('root', 'Invalid Name'):
            with patch.object(pwd, 'getpwuid', return_value=types.SimpleNamespace(pw_name=username, pw_gecos='')):
                self.assertEqual(local_user_identity(), ('', ''))
