"""Catalyst, Gentoo mirror, and QEMU architecture names."""
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.architecture import Architecture, Emulation


class ArchitectureTests(unittest.TestCase):
    def test_architecture_tables_cover_shared_catalyst_tables(self):
        self.assertEqual([table.name for table in Architecture.mips.catalyst_arch_tables()], ['mips', 'mips64'])
        self.assertEqual(Architecture.arm64.catalyst_arch_tables()[0].name, 'arm64')
        self.assertEqual(Architecture.ppc64.catalyst_arch_tables()[0].chost_prefix, 'powerpc64-')

    def test_release_folders_and_user_emulator_names(self):
        self.assertEqual(Architecture.ppc64le.releases_directory(), 'ppc')
        self.assertEqual(Architecture.s390x.releases_directory(), 's390')
        self.assertEqual(Architecture.ppc64.releng_base_arch().value, 'ppc/ppc64')
        self.assertIsNone(Architecture.mips.releng_base_arch())
        self.assertEqual(Architecture.arm64.qemu_user_binary(), '/usr/bin/qemu-aarch64')
        self.assertIsNone(Architecture.ia64.qemu_user_binary())

    def test_qemu_system_list_is_unique_and_sorted(self):
        systems = Emulation.get_all_qemu_systems()
        self.assertEqual(systems, sorted(set(systems)))
        self.assertIn('qemu-system-aarch64', systems)


if __name__ == '__main__':
    unittest.main()
