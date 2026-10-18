"""Generate Raspberry Pi templates without building Gentoo or touching a target machine."""
import os
from pathlib import Path
import subprocess
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

from catalystlab import app_info
from catalystlab.project_template import ProjectTemplate, _copy_template_files


class RaspberryPiPlasmaTests(unittest.TestCase):
    def setUp(self):
        version = patch.object(app_info, 'APP_VERSION', '2.0.0')
        version.start()
        self.addCleanup(version.stop)
        self.template = ProjectTemplate.load(str(ROOT / 'data/project_templates/raspberry-pi-5'))

    def generate(self, directory, init):
        names = self.template.resolve(dict(init=init, extra_stages=['stage4', 'plasma', 'gnome', 'iso_gui']))
        generated = self.template.generate(names)
        for source, destination in generated.files:
            _copy_template_files(source, str(directory / destination), names, lambda _: None)
        return {stage.template_id: stage for stage in generated.stages}

    def test_stage4_leaves_kernel_packages_to_stage_configuration_and_preserves_desktop_setup(self):
        for init in ('systemd', 'openrc'):
            with self.subTest(init=init), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                stages = self.generate(directory, init)
                plasma = stages['stage4-plasma']
                for stage_id in ('stage4', 'stage4-gnome', 'stage4-plasma'):
                    stage = stages[stage_id]
                    packages = stages[stage_id].arguments['stage4_packages']
                    self.assertNotIn('sys-kernel/raspberrypi-image', packages)
                    self.assertNotIn('sys-kernel/raspberrypi-sources', packages)
                    self.assertNotIn('boot_kernel', stage.arguments)
                    self.assertNotIn('boot_kernels', stage.arguments)
                    if stage_id == 'stage4-plasma':
                        self.assertEqual(stage.arguments['stage4_fsscript'], '@STAGE_DIR@/configure-plasma.sh')
                    else:
                        self.assertNotIn('stage4_fsscript', stage.arguments)
                stage = directory / 'stages' / plasma.name
                script = stage / 'configure-plasma.sh'
                subprocess.run(['bash', '-n', str(script)], check=True)
                self.assertFalse((stage / 'kernel.config').exists())
                self.assertFalse((stage / 'install-kernel.sh').exists())
                self.assertFalse((stage / 'root_overlay/usr/local/libexec/catalystlab-install-kernel').exists())
                self.assertNotIn('catalystlab-install-kernel', script.read_text())
                vc4 = stage / 'root_overlay/etc/X11/xorg.conf.d/99-catalystlab-vc4.conf'
                self.assertIn('MatchDriver "vc4"', vc4.read_text())
                self.assertIn('Option "PrimaryGPU" "true"', vc4.read_text())
                self.assertNotIn('/dev/dri/card', vc4.read_text())
                if init == 'openrc':
                    self.assertIn('display-manager|default', plasma.arguments['stage4_rcadd'])
                # Execute only the configuration branch; mock systemctl so the host stays untouched.
                tools = directory / 'tools'
                tools.mkdir()
                command = tools / 'systemctl'
                command.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n')
                command.chmod(0o755)
                kernel_installer = tools / 'kernel-installer'
                kernel_installer.write_text('#!/bin/sh\nprintf "install-kernel\\n" >> "$CALL_LOG"\n')
                kernel_installer.chmod(0o755)
                log = directory / 'calls'
                subprocess.run(['bash', str(script)], check=True,
                    env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'], 'CALL_LOG': str(log),
                         'CATALYSTLAB_KERNEL_INSTALLER': str(kernel_installer)})
                self.assertEqual(log.read_text().splitlines() if log.exists() else [],
                    ['--root=/ enable sddm.service', '--root=/ set-default graphical.target']
                    if init == 'systemd' else [])

    def test_live_iso_does_not_receive_stage4_boot_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            stages = self.generate(directory, 'openrc')
            for name in ('stage1', 'stage3', 'stage3-desktop', 'livegui-stage1', 'livegui-stage2'):
                if name in stages:
                    stage = directory / 'stages' / stages[name].name
                    self.assertFalse((stage / 'configure-plasma.sh').exists())
                    self.assertFalse((stage / 'root_overlay/etc/X11/xorg.conf.d/99-catalystlab-vc4.conf').exists())
            self.assertNotIn('boot_kernel', stages['livegui-stage2'].arguments)
            self.assertNotIn('boot_kernels', stages['livegui-stage2'].arguments)


if __name__ == '__main__':
    unittest.main()
