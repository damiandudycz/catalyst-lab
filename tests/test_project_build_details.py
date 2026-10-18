"""Parsing of build failure details and installed package lists."""
from pathlib import Path
import gzip
import sys
import tempfile
import types
import unittest
import subprocess

ROOT = Path(__file__).resolve().parents[1]
if "catalystlab" not in sys.modules:
    package = types.ModuleType("catalystlab")
    package.__path__ = [str(ROOT / "src")] + [str(path) for path in (ROOT / "src").rglob("*") if path.is_dir()]
    sys.modules["catalystlab"] = package

from catalystlab.project_build_details import (
    _project_commit, failure_details, failure_logs_script, installed_packages, output_details,
)


class ProjectBuildDetailsTests(unittest.TestCase):
    def test_reads_commit_and_working_tree_state_from_one_git_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(["git", "init", "-q", temporary], check=True)
            subprocess.run(["git", "-C", temporary, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", temporary, "config", "user.email", "test@example.com"], check=True)
            Path(temporary, "file").write_text("committed")
            subprocess.run(["git", "-C", temporary, "add", "file"], check=True)
            subprocess.run(["git", "-C", temporary, "commit", "-qm", "initial"], check=True)
            project = types.SimpleNamespace(directory_path=lambda: temporary)

            clean = _project_commit(project)
            Path(temporary, "file").write_text("modified")
            dirty = _project_commit(project)

        self.assertEqual(clean["commit"], dirty["commit"])
        self.assertFalse(clean["modified"])
        self.assertTrue(dirty["modified"])

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

    def test_extracts_compiler_error_only_from_failed_package_report(self):
        failure = failure_details([
            "warning: error: optional feature probe failed, continuing",
            ">>> Failed to emerge media-libs/glycin-loaders-2.2.1, Log file:",
            " * Package: media-libs/glycin-loaders-2.2.1:2",
            "warning: error: optional feature probe failed, continuing",
            "aarch64-unknown-linux-gnu-gcc: fatal error: cannot execute ‘as’: posix_spawnp: Permission denied",
            "compilation terminated.",
            " * ERROR: media-libs/glycin-loaders-2.2.1::gentoo failed (configure phase):",
        ], None)

        self.assertEqual(failure["reason"], "error: cannot execute ‘as’: posix_spawnp: Permission denied")

    def test_does_not_promote_warning_or_error_outside_failed_package_report(self):
        failure = failure_details([
            "warning: error: optional feature probe failed, continuing",
            ">>> Failed to emerge media-libs/glycin-loaders-2.2.1, Log file:",
            "warning: error: optional feature probe failed, continuing",
            "configure completed successfully",
            " * ERROR: media-libs/glycin-loaders-2.2.1::gentoo failed (configure phase):",
        ], None)

        self.assertIsNone(failure["reason"])

    def test_failure_logs_capture_assembler_access_for_portage_and_mounts(self):
        script = failure_logs_script('/stage/root', '/build/failure')

        self.assertIn('gcc -print-prog-name=as', script)
        self.assertIn('stat -Lc', script)
        self.assertIn('cat /proc/self/mountinfo', script)
        self.assertIn('namei -l "$AS"', script)
        self.assertIn('executing assembler as Portage user', script)
        self.assertIn('os.setuid(account.pw_uid)', script)

    def test_reads_unique_installed_packages_from_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "CONTENTS.gz")
            with gzip.open(path, "wt", encoding="utf-8") as file:
                file.write("drwxr-xr-x root/root 0 2026-10-17 10:00 ./var/db/pkg/sys-apps/portage-3.0.80/\n")
                file.write("drwxr-xr-x root/root 0 2026-10-17 10:00 ./var/db/pkg/sys-apps/portage-3.0.80/\n")
                file.write("drwxr-xr-x root/root 0 2026-10-17 10:00 ./var/db/pkg/sys-libs/glibc-2.42/\n")

            self.assertEqual(installed_packages(str(path)), ["sys-apps/portage-3.0.80", "sys-libs/glibc-2.42"])

    def test_output_details_save_archive_size_and_installed_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary, "stage.tar.xz")
            artifact.write_bytes(b"archive")
            with gzip.open(f"{artifact}.CONTENTS.gz", "wt", encoding="utf-8") as file:
                file.write("drwxr-xr-x root/root 0 2026-10-17 10:00 ./var/db/pkg/sys-apps/portage-3.0.80/\n")
            build = types.SimpleNamespace(artifact_path=str(artifact), path=temporary)

            details = output_details(build)

            self.assertEqual(details, {"size": len(b"archive"), "packages": 1})
            self.assertEqual(Path(temporary, "packages.txt").read_text(), "sys-apps/portage-3.0.80\n")


if __name__ == "__main__":
    unittest.main()
