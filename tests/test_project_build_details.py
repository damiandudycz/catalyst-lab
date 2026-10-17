"""Parsing of build failure details and installed package lists."""
from pathlib import Path
import gzip
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if "catalystlab" not in sys.modules:
    package = types.ModuleType("catalystlab")
    package.__path__ = [str(ROOT / "src")] + [str(path) for path in (ROOT / "src").rglob("*") if path.is_dir()]
    sys.modules["catalystlab"] = package

from catalystlab.project_build_details import failure_details, installed_packages


class ProjectBuildDetailsTests(unittest.TestCase):
    def test_extracts_failed_package_reason_and_available_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            failure_path = Path(temporary, "failure")
            failure_path.mkdir()
            (failure_path / "summary.txt").write_text("logs")
            failure = failure_details([
                "\x1b[31m * ERROR: sys-kernel/gentoo-kernel-6.1::gentoo failed (compile phase):\x1b[0m",
                " * ERROR: sys-kernel/gentoo-kernel-6.1::overlay failed (install phase):",
                "fatal error: Killed signal terminated program cc1plus",
            ], str(failure_path))

        self.assertEqual(failure, {
            "packages": [{"package": "sys-kernel/gentoo-kernel-6.1", "repository": "gentoo", "phase": "compile"}],
            "reason": "out of memory, compiler was killed",
            "logs": "failure",
        })

    def test_reads_unique_installed_packages_from_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "CONTENTS.gz")
            with gzip.open(path, "wt", encoding="utf-8") as file:
                file.write("drwxr-xr-x root/root 0 2026-10-17 10:00 ./var/db/pkg/sys-apps/portage-3.0.80/\n")
                file.write("drwxr-xr-x root/root 0 2026-10-17 10:00 ./var/db/pkg/sys-apps/portage-3.0.80/\n")
                file.write("drwxr-xr-x root/root 0 2026-10-17 10:00 ./var/db/pkg/sys-libs/glibc-2.42/\n")

            self.assertEqual(installed_packages(str(path)), ["sys-apps/portage-3.0.80", "sys-libs/glibc-2.42"])


if __name__ == "__main__":
    unittest.main()
