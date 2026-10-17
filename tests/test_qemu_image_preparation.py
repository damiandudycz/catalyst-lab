import tempfile
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import qemu_run


class KernelOutputReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.output = Path(self.temporary_directory.name)
        (self.output / "custom-kernel").write_bytes(b"kernel")
        (self.output / "custom-initramfs").write_bytes(b"initramfs")
        (self.output / qemu_run.KERNEL_OUTPUTS_MANIFEST).write_text("sys-kernel/gentoo-kernel\n6.18.57-gentoo\n")
        (self.output / "custom-modules.tar").write_bytes(b"module archive")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_ready_when_kernel_initramfs_and_modules_exist(self):
        self.assertTrue(qemu_run._kernel_outputs_ready(str(self.output), "sys-kernel/gentoo-kernel"))

    def test_manifest_must_match_requested_package(self):
        self.assertFalse(qemu_run._kernel_outputs_ready(str(self.output), "sys-kernel/other-kernel"))

    def test_modules_must_match_release_in_manifest(self):
        (self.output / "custom-modules.tar").unlink()
        self.assertFalse(qemu_run._kernel_outputs_ready(str(self.output), "sys-kernel/gentoo-kernel"))

    def test_missing_manifest_means_outputs_are_not_reusable(self):
        (self.output / qemu_run.KERNEL_OUTPUTS_MANIFEST).unlink()
        self.assertFalse(qemu_run._kernel_outputs_ready(str(self.output), "sys-kernel/gentoo-kernel"))

    def test_not_ready_when_any_required_output_is_missing(self):
        (self.output / "custom-initramfs").unlink()
        self.assertFalse(qemu_run._kernel_outputs_ready(str(self.output), "sys-kernel/gentoo-kernel"))

    def test_reusable_kernel_output_does_not_require_backend_specific_header(self):
        (self.output / "custom-kernel").write_bytes(b"backend-neutral kernel output")
        self.assertTrue(qemu_run._kernel_outputs_ready(str(self.output), "sys-kernel/gentoo-kernel"))


if __name__ == "__main__":
    unittest.main()
