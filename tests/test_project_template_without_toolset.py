"""Templates store declared stage values without requiring a Catalyst toolset."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
import uuid
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import app_info
from catalystlab.project_template import ProjectTemplate, apply_project_template


class ProjectTemplateWithoutToolsetTests(unittest.TestCase):
    def test_apply_keeps_template_values_and_does_not_assign_a_toolset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_dir = root / 'template'
            template_dir.mkdir()
            (template_dir / 'template.toml').write_text('''format = 1
name = "Bare template"
updated_at = "2025-01-01T00:00:00Z"
[[stages]]
id = "stage1"
name = "stage1-openrc"
target = "stage1"
[stages.last_changed]
name = "2025-02-01T00:00:00Z"
target = "2025-02-01T00:00:00Z"
common_flags = "2025-03-01T00:00:00Z"
[stages.arguments]
common_flags = "-O2 -pipe -march=armv8-a"
''', encoding='utf-8')
            project_path = root / 'project'
            project_path.mkdir()

            class ProjectDirectory:
                def __init__(self):
                    self._path = project_path
                    self._stages = []

                def directory_path(self):
                    return str(self._path)

                def stage_directory_path(self, name):
                    return str(self._path / 'stages' / name)

                @property
                def stages(self):
                    return self.__dict__.setdefault('_stages', [])

                def get_releng_directory(self):
                    return None

                def get_architecture(self):
                    return None

            class Stage:
                def __init__(self, id=None, **kwargs):
                    self.id = id or uuid.uuid4()
                    self.__dict__.update(kwargs)
                    self.target = kwargs['target_name']

            template = ProjectTemplate.load(str(template_dir))
            project = ProjectDirectory()
            defaults = Mock(side_effect=AssertionError('defaults must wait until a toolset is selected'))
            stage_module = types.ModuleType('catalystlab.project_stage')
            stage_module.ProjectStage = Stage
            stage_module.refresh_default_stage_arguments = defaults
            stage_module.remove_unsupported_parent_options = Mock(
                side_effect=AssertionError('validation must wait until a toolset is selected')
            )
            manager_module = types.ModuleType('catalystlab.project_manager')
            manager_module.ProjectManager = types.SimpleNamespace(
                shared=lambda: types.SimpleNamespace(save_stage=lambda **_kwargs: None)
            )
            confdir_module = types.ModuleType('catalystlab.project_stage_portage_confdir')
            confdir_module.remove_stage_overlays_inherited_files = lambda *_args: {}

            with patch.object(app_info, 'APP_VERSION', '2.0.0'), patch.dict(sys.modules, {
                stage_module.__name__: stage_module,
                manager_module.__name__: manager_module,
                confdir_module.__name__: confdir_module,
            }), patch('catalystlab.project_template._enable_stage_overlays'):
                apply_project_template(project, template, template.resolve({}), log=lambda _line: None,
                                       overlay_ids={})

            self.assertEqual(len(project.stages), 1)
            self.assertEqual(project.stages[0].common_flags, '-O2 -pipe -march=armv8-a')
            self.assertIsNone(getattr(project.stages[0], "toolset_id", None))
            self.assertEqual(project.stages[0]._explicit_user_argument_dates, {
                'name': '2025-02-01T00:00:00Z',
                'target': '2025-02-01T00:00:00Z',
                'common_flags': '2025-03-01T00:00:00Z',
            })
            defaults.assert_not_called()


if __name__ == '__main__':
    unittest.main()
