"""Deployment boot entries wait for their root device without a GUI delay option."""
from enum import Enum
import re
import subprocess
import types
import unittest
from test_deploy_services import load_function


class RootwaitTests(unittest.TestCase):
    def test_live_pi_adds_rootwait_once(self):
        command = load_function('src/objects/deploy/deploy_live_iso.py', 'raspberry_pi_cmdline')
        settings = types.SimpleNamespace(serial_console=True, extra_arguments='')
        for arguments in ('boot=live', 'boot=live rootwait'):
            self.assertEqual(command(arguments, settings).split().count('rootwait'), 1)
        settings.extra_arguments = 'rootwait quiet'
        self.assertEqual(command('boot=live', settings).split().count('rootwait'), 1)

    def test_all_installed_bootloaders_include_rootwait(self):
        bootloader = Enum('Bootloader', 'GRUB SYSTEMD_BOOT REFIND EFI_STUB KBOOT RASPBERRY_PI')
        kboot = load_function('src/objects/deploy/deploy_boot.py', 'kboot_script')
        pi = load_function('src/objects/deploy/deploy_boot.py', 'raspberry_pi_script')
        script = load_function('src/objects/deploy/deploy_boot.py', 'bootloader_script',
            Bootloader=bootloader, CHROOT_SETUP='', grub_target=lambda *args: 'x86_64-efi',
            kboot_script=kboot, raspberry_pi_script=pi, re=re)
        for loader in bootloader:
            with self.subTest(loader=loader):
                result = script(loader, 'arm64' if loader == bootloader.RASPBERRY_PI else 'amd64',
                                True, '/dev/sda', '/dev/sda2', '/dev/sda1', '/boot', ps3=True)
                self.assertIn('rootwait', result)
                subprocess.run(['bash', '-n'], input=result, text=True, check=True)
        self.assertIn('OPTIONS="$OPTIONS video=', kboot('/dev/sda2', False, ps3=True))
