"""Run with the app's Python environment: python -m unittest discover -s tests."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import uuid

# Meson installs these modules in one package; use the source directories in checkout tests.
ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(p) for p in (ROOT / 'src').rglob('*') if p.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import app_info
from catalystlab.project_template import ProjectTemplate, TemplateError, apply_project_template


class StageIdentityTests(unittest.TestCase):
    def setUp(self):
        self.version = patch.object(app_info, 'APP_VERSION', '2.0.0')
        self.version.start()
        self.addCleanup(self.version.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.template_path = self.root / 'template'
        self.template_path.mkdir()

    def template(self, suffix='{{ init }}'):
        text = '''format = 1
name = "Test"
[[variables]]
id = "init"
options = ["openrc", "systemd"]
[[stages]]
id = "stage1"
name = "stage1-{{ init }}"
target = "stage1"
[[stages]]
id = "stage3"
parent = "stage1"
name = "stage3-{{ init }}"
target = "stage3"
'''
        if suffix is not None:
            import json
            text = 'stage_id_suffix = ' + json.dumps(suffix) + '\n' + text
        (self.template_path / 'template.toml').write_text(text)
        return ProjectTemplate.load(str(self.template_path))

    def apply(self, template, init, previous=None):
        path = self.root / str(uuid.uuid4())
        path.mkdir()
        project = types.SimpleNamespace(
            directory_path=lambda: str(path), stages=[],
            stage_directory_path=lambda name: str(path / 'stages' / name),
            get_releng_directory=lambda: None, get_architecture=lambda: None,
        )
        class Stage:
            def __init__(self, id=None, **kwargs):
                self.id = id or uuid.uuid4()
                self.__dict__.update(kwargs)
                self.target = kwargs["target_name"]
        stage_module = types.ModuleType('catalystlab.project_stage')
        stage_module.ProjectStage = Stage
        stage_module.apply_default_stage_arguments = lambda **kwargs: None
        stage_module.remove_unsupported_parent_options = lambda *args, **kwargs: []
        manager_module = types.ModuleType('catalystlab.project_manager')
        manager_module.ProjectManager = types.SimpleNamespace(shared=lambda: types.SimpleNamespace(save_stage=lambda **kwargs: None))
        confdir_module = types.ModuleType('catalystlab.project_stage_portage_confdir')
        confdir_module.remove_stage_overlays_inherited_files = lambda *args: {}
        with patch.dict(sys.modules, {
            stage_module.__name__: stage_module, manager_module.__name__: manager_module,
            confdir_module.__name__: confdir_module,
        }), patch('catalystlab.project_template._stage_argument_names', return_value=None), patch('catalystlab.project_template._enable_stage_overlays'):
            ids = apply_project_template(project, template, template.resolve({'init': init}),
                                         log=lambda _: None, overlay_ids={}, stage_ids=previous)
        self.assertEqual(project.stages[1].parent_id, project.stages[0].id)
        return ids

    def test_switching_init_changes_uuids_but_same_init_preserves_them(self):
        template = self.template()
        openrc = self.apply(template, 'openrc')
        self.assertEqual(set(openrc), {'stage1:openrc', 'stage3:openrc'})
        self.assertEqual(openrc, self.apply(template, 'openrc', openrc))
        systemd = self.apply(template, 'systemd', openrc)
        self.assertEqual(set(systemd), {'stage1:systemd', 'stage3:systemd'})
        self.assertTrue(set(openrc.values()).isdisjoint(systemd.values()))
        self.assertEqual(systemd, self.apply(template, 'systemd', systemd))
        switched_back = self.apply(template, 'openrc', systemd)
        self.assertTrue(set(systemd.values()).isdisjoint(switched_back.values()))

    def test_adding_identity_suffix_changes_generated_stage_ids(self):
        unsuffixed = self.apply(self.template(suffix=None), 'openrc')
        self.assertEqual(set(unsuffixed), {'stage1', 'stage3'})
        self.assertEqual(unsuffixed, self.apply(self.template(suffix=None), 'openrc', unsuffixed))
        suffixed = self.apply(self.template(), 'openrc', unsuffixed)
        self.assertTrue(set(unsuffixed.values()).isdisjoint(suffixed.values()))

    def test_invalid_suffix_is_rejected(self):
        for suffix in ['', '{{ [] }}', '{{ True }}', '{{ None }}']:
            with self.subTest(suffix=suffix), self.assertRaises(TemplateError):
                template = self.template(suffix)
                template.generate(template.resolve({}))

    def test_official_templates_keep_original_parents_and_group_files(self):
        for path in (ROOT / 'data/project_templates').glob('*/template.toml'):
            template = ProjectTemplate.load(str(path.parent))
            for init in ['openrc', 'systemd']:
                with self.subTest(template=path.parent.name, init=init):
                    selection = {'init_systems': [init]} if any(variable.id == 'init_systems' for variable in template.variables) else {'init': init}
                    names = template.resolve({**selection, 'accept_all_licenses': True,
                                              'extra_stages': ['stage4', 'gnome', 'plasma', 'xfce', 'iso', 'iso_gui', 'livegui']})
                    with patch('catalystlab.project_template._template_argument_value',
                               side_effect=lambda _key, value, _context: value):
                        generated = template.generate(names)
                    stages = {s.template_id: s for s in generated.stages}
                    self.assertFalse(any('iso-seed' in key for key in stages))
                    template_id_suffix = f'-{init}' if 'init_systems' in selection else ''
                    persistent_id = lambda stage: f'{stage.template_id.removesuffix(template_id_suffix)}:{init}'
                    self.assertTrue(all(s.persistent_id == persistent_id(s) for s in stages.values()))
                    self.assertEqual(stages[f'stage3{template_id_suffix}'].parent, f'stage1{template_id_suffix}')
                    for child, parent in [('livecd-stage1', 'stage3'), ('livegui-stage1', 'stage3-desktop')]:
                        child_id = child + template_id_suffix
                        if child_id in stages:
                            self.assertEqual(stages[child_id].parent, parent + template_id_suffix)
                    if path.parent.name == 'gentoo-release':
                        self.assertTrue(any(dest.endswith('/portage') and 'stage4' in dest for _, dest in generated.files))


if __name__ == '__main__':
    unittest.main()
