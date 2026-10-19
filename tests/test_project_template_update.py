"""Project template version comparison and loading."""
from pathlib import Path
import sys
import subprocess
import json
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.project_template_update import (
    ProjectVersion, TemplateState, compare_versions, read_project_version, project_stage_baseline
)
from catalystlab.project_stage_baselines import STAGE_BASELINE_FILE, record_stage_creation_baseline
from catalystlab.project_template_update import _replay_stage_baselines


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

    def test_same_value_user_timestamp_conflicts_with_template_value_change(self):
        base = ProjectVersion(
            entries={"stage:stage-id#subarch": {"type": "str", "value": "amd64"}},
            stage_property_dates={"stage-id": {"subarch": "2026-10-19T00:00:00Z"}},
        )
        project = ProjectVersion(
            entries={"stage:stage-id#subarch": {"type": "str", "value": "amd64"}},
            stage_property_dates={"stage-id": {"subarch": "2026-10-19T12:00:00Z"}},
        )
        template = ProjectVersion(
            entries={"stage:stage-id#subarch": {"type": "str", "value": "arm64"}},
            stage_property_dates={"stage-id": {"subarch": "2026-10-20T00:00:00Z"}},
        )

        changes = compare_versions(base, project, template)

        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0].conflict)
        self.assertFalse(changes[0].use_template)

    def test_removing_old_template_timestamp_reports_date_only_change(self):
        base = ProjectVersion(
            entries={'stage:stage-id#subarch': {'type': 'str', 'value': 'amd64'}},
            stage_names={'stage-id': 'stage1'},
            stage_property_dates={'stage-id': {'subarch': '2026-10-19T00:00:00Z'}},
        )
        project = ProjectVersion(
            entries={'stage:stage-id#subarch': {'type': 'str', 'value': 'amd64'}},
            stage_names={'stage-id': 'stage1'},
            stage_property_dates={'stage-id': {'subarch': '2026-10-19T00:00:00Z'}},
        )
        template = ProjectVersion(
            entries={'stage:stage-id#subarch': {'type': 'str', 'value': 'amd64'}},
            stage_names={'stage-id': 'stage1'},
            stage_property_dates={},
        )

        changes = compare_versions(base, project, template)

        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0].date_only)
        self.assertFalse(changes[0].conflict)
        self.assertEqual(changes[0].base, changes[0].template)
        self.assertEqual(changes[0].base_date, '2026-10-19T00:00:00Z')
        self.assertNotEqual(changes[0].template_date, changes[0].base_date)

    def test_comparison_can_list_changes_from_current_project_to_clean_result(self):
        current = ProjectVersion(entries={"README.md": b"local", "setting": b"same"})
        clean = ProjectVersion(entries={"README.md": b"template", "setting": b"same"})

        changes = compare_versions(current, current, clean)

        self.assertEqual([change.key for change in changes], ["README.md"])
        self.assertFalse(changes[0].conflict)
        self.assertEqual(changes[0].project, b"local")
        self.assertEqual(changes[0].template, b"template")

    def test_reader_keys_stage_settings_and_files_by_stage_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / 'stages' / 'stage4-systemd'
            stage.mkdir(parents=True)
            (stage / 'stage.json').write_text(
                '{"id":{"value":"stage-id"},"name":{"value":"Stage 4"},'
                '"_catalystlab":{"explicit":["name"]}}', encoding='utf-8'
            )
            (stage / 'package.use').write_bytes(b'feature\n')
            (stage / STAGE_BASELINE_FILE).write_text('{"id":{"value":"stage-id"}}', encoding='utf-8')
            (root / 'README.md').write_bytes(b'project')
            (root / 'catalystlab-stage-overrides.json').write_text(
                '{"stage-id":{"defaults":{},"explicit":[]}}', encoding='utf-8'
            )
            (stage / 'ignored-link').symlink_to(stage / 'package.use')

            version = read_project_version(str(root))

        self.assertEqual(version.stage_directories, {'stage-id': 'stage4-systemd'})
        self.assertEqual(version.stage_names, {'stage-id': 'Stage 4'})
        self.assertEqual(version.entries['stage:stage-id#id'], {'value': 'stage-id'})
        self.assertEqual(version.stage_property_dates, {})
        self.assertEqual(version.stage_baselines, {'stage-id': {'id': {'value': 'stage-id'}}})
        self.assertNotIn('stage:stage-id#_catalystlab', version.entries)
        self.assertEqual(version.entries['stage:stage-id/package.use'], b'feature\n')
        self.assertEqual(version.entries['README.md'], b'project')
        self.assertNotIn('catalystlab-stage-overrides.json', version.entries)
        self.assertNotIn(f'stage:stage-id/{STAGE_BASELINE_FILE}', version.entries)
        self.assertNotIn('stage:stage-id/ignored-link', version.entries)

    def test_reader_extracts_property_timestamps_without_including_them_in_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage_dir = Path(temporary) / 'stages' / 'stage1'
            stage_dir.mkdir(parents=True)
            (stage_dir / 'stage.json').write_text(json.dumps({
                'id': {'type': 'UUID', 'value': 'stage-id'},
                'subarch': {
                    'type': 'str', 'value': 'amd64',
                    'last_changed': '2026-10-19T20:00:00+00:00',
                },
                'profile': {'type': 'NoneType', 'value': None, 'last_changed': None},
            }), encoding='utf-8')

            version = read_project_version(temporary)

        self.assertEqual(version.stage_property_dates, {
            'stage-id': {'subarch': '2026-10-19T20:00:00+00:00'},
        })
        self.assertEqual(version.entries['stage:stage-id#subarch'], {'type': 'str', 'value': 'amd64'})
        self.assertEqual(version.entries['stage:stage-id#profile'], {'type': 'NoneType', 'value': None})

    def test_stage_baseline_keeps_local_commits_customized_for_cloned_projects(self):
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
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.com', 'commit', '-qm', 'local customization'], check=True)

            baseline = project_stage_baseline(str(root), 'stage-id')

        self.assertEqual(baseline['name'], {'type': 'str', 'value': 'Original'})

    def test_local_project_baseline_uses_first_commit_containing_stage_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True)
            stage_dir = root / 'stages' / 'stage1'
            stage_dir.mkdir(parents=True)
            stage_file = stage_dir / 'stage.json'
            stage_file.write_text(
                '{"id":{"type":"str","value":"stage-id"},'
                '"name":{"type":"str","value":"Created by wizard"}}', encoding='utf-8'
            )
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.com', 'commit', '-qm', 'create stage'], check=True)
            stage_file.write_text(
                '{"id":{"type":"str","value":"stage-id"},'
                '"name":{"type":"str","value":"Later edit"}}', encoding='utf-8'
            )

            baseline = project_stage_baseline(str(root), 'stage-id')

        self.assertEqual(baseline['name'], {'type': 'str', 'value': 'Created by wizard'})

    def test_template_project_user_added_stage_uses_its_first_commit_as_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True)
            template_stage_dir = root / 'stages' / 'template-stage'
            template_stage_dir.mkdir(parents=True)
            (template_stage_dir / 'stage.json').write_text(
                '{"id":{"type":"str","value":"template-id"},'
                '"name":{"type":"str","value":"Template"}}', encoding='utf-8'
            )
            (root / 'project-template.json').write_text(json.dumps({
                'format': 1, 'repository_url': 'https://example.invalid/template.git',
                'repository_path': '', 'source_kind': 'repository', 'template_name': 'Test',
                'commit': 'template-commit', 'selected': {}, 'stage_ids': {}, 'template_tree': None,
            }), encoding='utf-8')
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.com', 'commit', '-qm',
                            'Create project from template Test'], check=True)
            added_stage_dir = root / 'stages' / 'user-stage'
            added_stage_dir.mkdir()
            added_stage = added_stage_dir / 'stage.json'
            added_stage.write_text(
                '{"id":{"type":"str","value":"user-id"},'
                '"name":{"type":"str","value":"Added by user"}}', encoding='utf-8'
            )
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.com', 'commit', '-qm', 'add user stage'], check=True)

            baseline = project_stage_baseline(str(root), 'user-id')

        self.assertEqual(baseline['name'], {'type': 'str', 'value': 'Added by user'})

    def test_uncommitted_stage_uses_creation_wizard_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True)
            stage_dir = root / 'stages' / 'stage1'
            stage_dir.mkdir(parents=True)
            stage = {'id': {'type': 'str', 'value': 'stage-id'},
                     'name': {'type': 'str', 'value': 'stage1'},
                     'releng_template': {'type': 'NoneType', 'value': None}}
            (stage_dir / 'stage.json').write_text(json.dumps(stage), encoding='utf-8')
            record_stage_creation_baseline(str(stage_dir), stage)

            baseline = project_stage_baseline(str(root), 'stage-id')

        self.assertEqual(baseline['name'], {'type': 'str', 'value': 'stage1'})
        self.assertEqual(baseline['__catalystlab_baseline_kind__'], 'initial_snapshot')

    def test_stage_creation_snapshots_survive_template_rebase(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage_dir = root / 'stages' / 'stage1'
            stage_dir.mkdir(parents=True)
            parent = ProjectVersion(stage_directories={'stage-id': 'stage1'})
            created = ProjectVersion(stage_baselines={
                'stage-id': {'name': {'type': 'str', 'value': 'stage1'}}
            }, stage_directories={'stage-id': 'stage1'})

            _replay_stage_baselines(str(root), parent, created)

            persisted = json.loads((stage_dir / STAGE_BASELINE_FILE).read_text(encoding='utf-8'))

        self.assertEqual(persisted['name'], {'type': 'str', 'value': 'stage1'})

    def test_stage_baseline_uses_source_branch_when_local_and_origin_diverge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True)
            stage_dir = root / 'stages' / 'stage4'
            stage_dir.mkdir(parents=True)
            stage_file = stage_dir / 'stage.json'
            stage_file.write_text(
                '{"id":{"type":"str","value":"stage-id"},'
                '"name":{"type":"str","value":"Original"}}', encoding='utf-8'
            )
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.com', 'commit', '-qm', 'base'], check=True)
            base = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
            subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin', 'https://example.invalid/project.git'], check=True)

            stage_file.write_text(
                '{"id":{"type":"str","value":"stage-id"},'
                '"name":{"type":"str","value":"Local customization"}}', encoding='utf-8'
            )
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.com', 'commit', '-qm', 'local customization'], check=True)

            # Simulate the source branch advancing independently from the local commit.
            subprocess.run(['git', '-C', str(root), 'checkout', '-q', '-b', 'source', base], check=True)
            stage_file.write_text(
                '{"id":{"type":"str","value":"stage-id"},'
                '"name":{"type":"str","value":"Origin value"}}', encoding='utf-8'
            )
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.com', 'commit', '-qm', 'origin update'], check=True)
            origin_commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
            subprocess.run(['git', '-C', str(root), 'update-ref', 'refs/remotes/origin/main', origin_commit], check=True)
            subprocess.run(['git', '-C', str(root), 'checkout', '-q', 'main'], check=True)
            subprocess.run(['git', '-C', str(root), 'branch', '--set-upstream-to=origin/main', 'main'],
                           check=True, stdout=subprocess.DEVNULL)

            baseline = project_stage_baseline(str(root), 'stage-id')

        self.assertEqual(baseline['name'], {'type': 'str', 'value': 'Origin value'})

    def test_template_baseline_is_not_replaced_by_local_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True)
            stage_dir = root / 'stages' / 'stage4'
            stage_dir.mkdir(parents=True)
            stage_file = stage_dir / 'stage.json'
            stage_file.write_text(
                '{"id":{"type":"str","value":"stage-id"},'
                '"name":{"type":"str","value":"Template value"}}', encoding='utf-8'
            )
            TemplateState(
                repository_url='https://example.invalid/templates.git', repository_path='templates/demo',
                template_name='Demo', commit='template-source-commit', selected={},
                stage_ids={'stage-id': '00000000-0000-0000-0000-000000000001'}, template_tree='tree',
            ).save(str(root))
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.com', 'commit', '-qm', 'Create project from template Demo'], check=True)

            stage_file.write_text(
                '{"id":{"type":"str","value":"stage-id"},'
                '"name":{"type":"str","value":"Committed local value"}}', encoding='utf-8'
            )
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.com', 'commit', '-qm', 'local customization'], check=True)

            baseline = project_stage_baseline(str(root), 'stage-id')

        self.assertEqual(baseline['name'], {'type': 'str', 'value': 'Template value'})

    def test_stage_baseline_is_empty_for_fresh_project_without_remote(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline = project_stage_baseline(temporary, 'stage-id')
        self.assertEqual(baseline, {})

if __name__ == '__main__':
    unittest.main()
