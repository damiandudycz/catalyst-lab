"""Empty passwords leave account authentication untouched."""
import unittest
import test_deploy_ssh_detection  # Initialize the source package used by standalone tests.
from catalystlab.deploy_installation import DeploySystemSettings, DeployUser, system_configuration_script


class OptionalPasswordTests(unittest.TestCase):
    def test_empty_passwords_do_not_call_chpasswd(self):
        settings = DeploySystemSettings(users=[DeployUser(name='alice', password='')])
        script = '\n'.join(system_configuration_script(settings))
        self.assertIn('useradd', script)
        self.assertNotIn('chpasswd', script)
        self.assertNotIn('passwd -d', script)

    def test_nonempty_passwords_are_still_applied(self):
        settings = DeploySystemSettings(root_password='root-test', users=[DeployUser(name='alice', password='alice-test')])
        script = '\n'.join(system_configuration_script(settings))
        self.assertEqual(script.count('chpasswd'), 2)
