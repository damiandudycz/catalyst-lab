"""SSH availability and enablement are read from the installed stage, not the host."""
import sys
from pathlib import Path
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(p) for p in (ROOT / 'src').rglob('*') if p.is_dir()]
    sys.modules['catalystlab'] = package
from catalystlab.deploy_boot import StageContents


class SSHDetectionTests(unittest.TestCase):
    def test_installed_is_distinct_from_enabled(self):
        contents = StageContents(init='systemd')
        self.assertFalse(contents.ssh_installed)
        contents._add_path('usr/sbin/sshd')
        self.assertTrue(contents.ssh_installed)
        self.assertFalse(contents.ssh_enabled)
        contents._add_path('etc/systemd/system/multi-user.target.wants/sshd.service', '/usr/lib/systemd/system/sshd.service')
        self.assertTrue(contents.ssh_enabled)

    def test_enablement_uses_target_init(self):
        contents = StageContents(init='systemd')
        contents._add_path('usr/sbin/sshd')
        contents._add_path('etc/runlevels/default/sshd', '/etc/init.d/sshd')
        self.assertFalse(contents.ssh_enabled)
        contents.init = 'openrc'
        self.assertTrue(contents.ssh_enabled)

    def test_service_file_alone_does_not_enable_ssh(self):
        contents = StageContents(init='systemd')
        contents._add_path('usr/sbin/sshd')
        contents._add_path('usr/lib/systemd/system/sshd.service')
        self.assertFalse(contents.ssh_enabled)

    def test_socket_activation_is_enabled(self):
        contents = StageContents(init='systemd', ssh_installed=True)
        contents._add_path('etc/systemd/system/sockets.target.wants/sshd.socket', '/usr/lib/systemd/system/sshd.socket')
        self.assertTrue(contents.ssh_enabled)
