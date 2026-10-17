"""Project template version comparison and loading."""
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.project_template_update import ProjectVersion, compare_versions, read_project_version


class ProjectTemplateUpdateTests(unittest.TestCase):
    def test_comparison_marks_template_only_changes_and_conflicts(self):
        base = ProjectVersion(entries={'README.md': b'base', 'config': b'base'})
        project = ProjectVersion(entries={'README.md': b'base', 'config': b'custom'})
        template = ProjectVersion(entries={'README.md': b'updated', 'config': b'updated'})

        changes = compare_versions(base, project, template)

        self.assertEqual([change.key for change in changes], ['README.md', 'config'])
        self.assertFalse(changes[0].conflict)
        self.assertTrue(changes[0].use_template)
        self.assertTrue(changes[1].conflict)
        self.assertFalse(changes[1].use_template)

    def test_reader_keys_stage_settings_and_files_by_stage_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / 'stages' / 'stage4-systemd'
            stage.mkdir(parents=True)
            (stage / 'stage.json').write_text(
                '{"id":{"value":"stage-id"},"name":{"value":"Stage 4"}}', encoding='utf-8'
            )
            (stage / 'package.use').write_bytes(b'feature\n')
            (root / 'README.md').write_bytes(b'project')
            (stage / 'ignored-link').symlink_to(stage / 'package.use')

            version = read_project_version(str(root))

        self.assertEqual(version.stage_directories, {'stage-id': 'stage4-systemd'})
        self.assertEqual(version.stage_names, {'stage-id': 'Stage 4'})
        self.assertEqual(version.entries['stage:stage-id#id'], {'value': 'stage-id'})
        self.assertEqual(version.entries['stage:stage-id/package.use'], b'feature\n')
        self.assertEqual(version.entries['README.md'], b'project')
        self.assertNotIn('stage:stage-id/ignored-link', version.entries)


if __name__ == '__main__':
    unittest.main()
