"""Library relocation preserves useful data while excluding transient files."""
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import library
from catalystlab.repository import resolve_path


class LibraryCopyTests(unittest.TestCase):
    def test_configured_path_expands_and_resolves_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'target'
            target.mkdir()
            alias = root / 'alias'
            alias.symlink_to(target, target_is_directory=True)

            self.assertEqual(resolve_path(str(alias)), str(target.resolve()))

    def test_copy_skips_temporary_and_preserves_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            destination = root / 'destination'
            project = source / 'Projects' / 'example'
            project.mkdir(parents=True)
            (project / 'metadata.json').write_text('{"name": "example"}')
            (project / 'link').symlink_to('metadata.json')
            (source / 'Temporary').mkdir()
            (source / 'Temporary' / 'in-progress').write_text('discard')
            progress = []

            library._copy_library(str(source), str(destination), lambda fraction, text: progress.append((fraction, text)))

            copied = destination / 'Projects' / 'example'
            self.assertEqual((copied / 'metadata.json').read_text(), '{"name": "example"}')
            self.assertTrue((copied / 'link').is_symlink())
            self.assertEqual((copied / 'link').readlink(), Path('metadata.json'))
            self.assertFalse((destination / 'Temporary').exists())
            self.assertEqual(progress[-1][0], 1)

    def test_failed_copy_removes_created_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            (source / 'file').write_text('data')
            destination = root / 'destination'

            with patch.object(library, '_copy_file', side_effect=OSError('copy failed')):
                with self.assertRaisesRegex(OSError, 'copy failed'):
                    library._copy_library(str(source), str(destination), lambda *_: None)

            self.assertFalse(destination.exists())

    def test_failed_copy_clears_existing_destination_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            (source / 'file').write_text('data')
            destination = root / 'destination'
            destination.mkdir()
            (destination / 'previous').write_text('old data')

            with patch.object(library, '_copy_file', side_effect=OSError('copy failed')):
                with self.assertRaisesRegex(OSError, 'copy failed'):
                    library._copy_library(str(source), str(destination), lambda *_: None)

            self.assertTrue(destination.is_dir())
            self.assertEqual(list(destination.iterdir()), [])

    def test_rewrite_paths_updates_library_paths_but_preserves_external_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / 'old-library'
            new = root / 'new-library'
            metadata = new / 'Projects' / 'example' / 'metadata.json'
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps({
                'path': str(old / 'Projects' / 'example'),
                'nested': [str(old / 'Cache' / 'file'), str(root / 'old-library-backup' / 'file')],
            }))

            library._rewrite_object_paths(str(new), str(old))

            values = json.loads(metadata.read_text())
            self.assertEqual(values['path'], str(new / 'Projects' / 'example'))
            self.assertEqual(values['nested'], [str(new / 'Cache' / 'file'), str(root / 'old-library-backup' / 'file')])


if __name__ == '__main__':
    unittest.main()
