"""Project template version comparison and loading."""
from pathlib import Path
import sys
import subprocess
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.project_template_update import (
    ProjectVersion, compare_versions, read_project_version, project_stage_baseline
)


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

    def test_stage_baseline_uses_git_upstream_for_cloned_projects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True)
            stage_dir = root / 'stages' / 'stage4'
            stage_dir.mkdir(parents=True)
            (stage_dir / 'stage.json').write_text(
                '{"id":{"type":"str","value":"stage-id"},'
                '"name":{"type":"str","value":"Original"}}', encoding='utf-8'
            )
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.com', 'commit', '-qm', 'base'], check=True)
            base = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
            subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin', 'https://example.invalid/project.git'], check=True)
            subprocess.run(['git', '-C', str(root), 'update-ref', 'refs/remotes/origin/main', base], check=True)
            subprocess.run(['git', '-C', str(root), 'branch', '--set-upstream-to=origin/main', 'main'],
                           check=True, stdout=subprocess.DEVNULL)
            (stage_dir / 'stage.json').write_text(
                '{"id":{"type":"str","value":"stage-id"},'
                '"name":{"type":"str","value":"Changed"}}', encoding='utf-8'
            )

            baseline = project_stage_baseline(str(root), 'stage-id')

        self.assertEqual(baseline['name'], {'type': 'str', 'value': 'Original'})

    def test_stage_baseline_is_empty_for_fresh_project_without_remote(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline = project_stage_baseline(temporary, 'stage-id')
        self.assertEqual(baseline, {})

if __name__ == '__main__':
    unittest.main()
