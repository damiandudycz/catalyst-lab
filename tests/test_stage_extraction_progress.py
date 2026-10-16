"""Real archive extraction reports bytes and only reaches 100 after tar succeeds."""
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import test_local_wifi
from catalystlab.qemu_run import stage_extract_script


class ExtractionProgressTests(unittest.TestCase):
    def test_extracts_archive_and_reports_completion(self):
        # Generated command targets GNU tar in the Linux image-building environment.
        tar = '/opt/homebrew/bin/gtar' if Path('/opt/homebrew/bin/gtar').exists() else 'tar'
        version = subprocess.run([tar, '--version'], capture_output=True, text=True)
        if 'GNU tar' not in version.stdout:
            self.skipTest('GNU tar is needed for extended-attribute extraction options')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'payload'
            source.write_bytes(os.urandom(3 * 1024 * 1024))
            archive = root / 'stage.tar.gz'
            with tarfile.open(archive, 'w:gz') as output:
                output.add(source, arcname='payload')
            target = root / 'target'
            target.mkdir()
            tools = root / 'tools'
            tools.mkdir()
            (tools / 'tar').symlink_to(tar if tar.startswith('/') else subprocess.check_output(['which', 'tar'], text=True).strip())
            command = stage_extract_script(str(archive))
            result = subprocess.run(['bash', '-c', command], capture_output=True, text=True,
                env={**os.environ, 'MNT': str(target), 'PATH': str(tools) + os.pathsep + os.environ['PATH']})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((target / 'payload').read_bytes(), source.read_bytes())
            values = [int(line.split()[-1]) for line in result.stdout.splitlines() if line.startswith('@@extract-progress ')]
            self.assertGreater(len(values), 1)
            self.assertEqual(values[-1], 100)
            self.assertEqual(values, sorted(values))
