"""Image planning must not connect or start a VM before installation."""
import os
import ast
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from test_deploy_services import load_function
import test_local_wifi
from catalystlab import deploy_image


class ImagePlanningTests(unittest.TestCase):
    def test_planning_does_not_connect(self):
        connection = Mock()
        target = Mock()
        path = Path(__file__).resolve().parents[1] / 'src/ui/deploy/deploy_create_view.py'
        tree = ast.parse(path.read_text())
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_prepare_image')
        namespace = dict(os=os, image_connection=Mock(return_value=connection), TargetMachine=target,
                         image_path=lambda build: '/temporary/test.img', initial_image_size=lambda size: 10)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
        plan = namespace['_prepare_image']
        view = types.SimpleNamespace(local_architecture='aarch64', build=types.SimpleNamespace(artifact_path=None),
            project_directory=object(), _firmware=lambda: 'UEFI', _set_machine=Mock())
        plan(view)
        connection.connect.assert_not_called()
        target.for_image.assert_called_once_with(connection, 'aarch64', 'UEFI', '/temporary/test.img', 10)

    def test_working_images_are_unique_and_in_temporary(self):
        from catalystlab.repository import Repository
        with tempfile.TemporaryDirectory() as temporary:
            settings = types.SimpleNamespace(value=types.SimpleNamespace(temporary_location=temporary))
            with patch.object(Repository, 'Settings', settings, create=True):
                build = types.SimpleNamespace(stage_name='stage4', path='/builds/stage4')
                first, second = deploy_image.image_path(build), deploy_image.image_path(build)
                self.assertNotEqual(first, second)
                self.assertTrue(Path(first).is_relative_to(Path(temporary).resolve()))
                self.assertIn('mkdir -p', deploy_image.create_image_script(first, 1024))
