"""Execute generated deployment scripts against disposable filesystem trees."""
import ast
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import types
import unittest
import sys
import tarfile
import io

ROOT = Path(__file__).resolve().parents[1]


def load_function(path, name, **namespace):
    tree = ast.parse((ROOT / path).read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
    return namespace[name]


class DeployServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='catalystlab-deploy-test-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.detect = load_function('src/objects/deploy/deploy_system.py', 'init_system_script')
        self.enable = load_function('src/objects/deploy/deploy_system.py', 'enable_service_script', init_system_script=self.detect)
        self.configure = load_function('src/objects/deploy/deploy_installation.py', 'system_configuration_script',
            quote=shlex.quote, init_system_script=self.detect, localization_script=lambda _: '', authorized_keys_script=lambda *args: '')

    def file(self, path, executable=False):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('')
        if executable:
            file.chmod(0o755)
        return file

    def execute(self, script):
        # Deploy uses set -e. A false optional-service check must not abort it.
        result = subprocess.run(['sh', '-ec', script + '\necho CONFIGURED'], env={**os.environ, 'ROOT': str(self.root)},
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CONFIGURED', result.stdout)
        return result.stdout

    def ssh_script(self):
        settings = types.SimpleNamespace(hostname=None, root_password=None, enable_ssh=True,
                                         localization=None, users=[], ssh_keys=[])
        return '\n'.join(self.configure(settings))

    def test_ssh_creates_missing_openrc_runlevel_and_can_run_again(self):
        self.file('sbin/openrc-run', executable=True)
        self.file('etc/init.d/sshd')
        script = self.ssh_script()
        for _ in range(2):
            self.assertIn('SSH server enabled (OpenRC)', self.execute(script))
            self.assertEqual(os.readlink(self.root / 'etc/runlevels/default/sshd'), '/etc/init.d/sshd')

    def test_ssh_without_package_warns_without_claiming_success(self):
        self.file('sbin/openrc-run', executable=True)
        output = self.execute(self.ssh_script())
        self.assertIn('SSH server is not installed', output)
        self.assertNotIn('SSH server enabled', output)
        self.assertFalse((self.root / 'etc/runlevels/default/sshd').is_symlink())

    def test_ssh_still_enables_systemd_service(self):
        self.file('usr/lib/systemd/system/sshd.service')
        self.assertIn('SSH server enabled (systemd)', self.execute(self.ssh_script()))
        self.assertEqual(os.readlink(self.root / 'etc/systemd/system/multi-user.target.wants/sshd.service'),
                         '/usr/lib/systemd/system/sshd.service')

    def test_other_services_create_missing_default_runlevel(self):
        (self.root / 'etc/runlevels').mkdir(parents=True)
        self.file('etc/init.d/dhcpcd')
        self.execute(self.enable('dhcpcd', 'dhcpcd'))
        self.assertEqual(os.readlink(self.root / 'etc/runlevels/default/dhcpcd'), '/etc/init.d/dhcpcd')

    def test_absent_optional_service_does_not_abort_deploy(self):
        self.execute(self.enable('dhcpcd', None))
        self.execute(self.enable('dhcpcd', 'dhcpcd'))

    def test_systemd_target_on_openrc_livecd_ignores_compatibility_files(self):
        self.file('etc/init.d/sshd')
        self.file('sbin/openrc-run', executable=True)
        (self.root / 'etc/runlevels').mkdir()
        self.file('usr/lib/systemd/system/sshd.service')
        (self.root / 'usr/bin').mkdir()
        # Absolute link is read as target metadata, not followed on the live CD.
        (self.root / 'usr/bin/init').symlink_to('/usr/lib/systemd/systemd')
        output = self.execute(self.ssh_script())
        self.assertIn('Target init system: systemd', output)
        self.assertIn('SSH server enabled (systemd)', output)
        self.assertFalse((self.root / 'etc/runlevels/default').exists())
        self.assertTrue((self.root / 'etc/systemd/system/multi-user.target.wants/sshd.service').is_symlink())

    def test_systemd_binary_wins_over_leftover_runlevels_for_shared_services(self):
        self.file('usr/lib/systemd/systemd', executable=True)
        self.file('usr/lib/systemd/system/dhcpcd.service')
        self.file('etc/init.d/dhcpcd')
        (self.root / 'etc/runlevels').mkdir()
        output = self.execute(self.enable('dhcpcd', 'dhcpcd'))
        self.assertIn('Enabled dhcpcd (systemd)', output)
        self.assertNotIn('(OpenRC)', output)
        self.assertFalse((self.root / 'etc/runlevels/default').exists())

    def test_explicit_openrc_init_wins_over_unused_systemd_binary(self):
        self.file('usr/lib/systemd/systemd', executable=True)
        self.file('etc/init.d/sshd')
        (self.root / 'usr/bin').mkdir()
        (self.root / 'usr/bin/init').symlink_to('/sbin/openrc-init')
        self.assertIn('SSH server enabled (OpenRC)', self.execute(self.ssh_script()))

    def test_archive_scan_uses_init_symlink_regardless_of_file_order(self):
        # Source modules are normally installed into one package by Meson.
        if 'catalystlab' not in sys.modules:
            package = types.ModuleType('catalystlab')
            package.__path__ = [str(ROOT / 'src')] + [str(p) for p in (ROOT / 'src').rglob('*') if p.is_dir()]
            sys.modules['catalystlab'] = package
        from catalystlab.deploy_boot import StageContents
        for init in ('systemd', 'openrc-init'):
            archive = self.root / (init + '.tar')
            with tarfile.open(archive, 'w') as tar:
                for name in ('usr/lib/systemd/systemd', 'usr/bin/openrc-run'):
                    member = tarfile.TarInfo(name)
                    member.size = 1
                    tar.addfile(member, io.BytesIO(b'x'))
                member = tarfile.TarInfo('usr/bin/init')
                member.type = tarfile.SYMTYPE
                member.linkname = '/usr/lib/systemd/systemd' if init == 'systemd' else '/sbin/openrc-init'
                tar.addfile(member)
            self.assertEqual(StageContents.scan(str(archive)).init, 'systemd' if init == 'systemd' else 'openrc')


if __name__ == '__main__':
    unittest.main()
