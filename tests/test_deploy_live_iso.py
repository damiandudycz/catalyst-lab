"""Parsing and boot settings for live ISO deployment."""
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if "catalystlab" not in sys.modules:
    package = types.ModuleType("catalystlab")
    package.__path__ = [str(ROOT / "src")] + [str(path) for path in (ROOT / "src").rglob("*") if path.is_dir()]
    sys.modules["catalystlab"] = package

from catalystlab.deploy_live_iso import (
    RaspberryPiBootSettings,
    parse_grub_entry,
    raspberry_pi_cmdline,
    raspberry_pi_config,
)


class LiveIsoBootSettingsTests(unittest.TestCase):
    def test_parse_first_grub_entry_with_optional_initramfs(self):
        config = """menuentry 'Gentoo' {
    linux /boot/vmlinuz root=UUID=abc quiet
    initrd /boot/initramfs.img
}
menuentry 'Other' {
    linux /other
}
"""
        self.assertEqual(parse_grub_entry(config), ("/boot/vmlinuz", "root=UUID=abc quiet", "/boot/initramfs.img"))
        self.assertEqual(parse_grub_entry("linux /Image console=ttyS0\n"), ("/Image", "console=ttyS0", None))

    def test_raspberry_pi_config_includes_kernel_initramfs_and_custom_lines(self):
        settings = RaspberryPiBootSettings(config_lines="dtparam=audio=on\n\ncustom=value")
        config = raspberry_pi_config("kernel_2712.img", "initramfs", settings)
        self.assertIn("kernel=kernel_2712.img", config)
        self.assertIn("initramfs initramfs followkernel", config)
        self.assertIn("custom=value", config)
        self.assertNotIn("\n\n", config)

    def test_cmdline_adds_only_missing_console_and_rootwait_arguments(self):
        settings = RaspberryPiBootSettings(extra_arguments="debug")
        self.assertEqual(raspberry_pi_cmdline("root=PARTUUID=abc", settings),
                         "console=serial0,115200 console=tty1 rootwait root=PARTUUID=abc debug\n")
        self.assertEqual(raspberry_pi_cmdline("console=ttyS0 rootwait", settings),
                         "console=ttyS0 rootwait debug\n")


if __name__ == "__main__":
    unittest.main()
