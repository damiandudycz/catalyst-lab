"""Malformed stage files stay visible and retain recoverable identity."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.project_directory import ProjectDirectory


class ProjectDirectoryStageRecoveryTests(unittest.TestCase):
    def test_malformed_stage_is_recovered_from_the_text_that_failed_to_parse(self):
        stage_id, parent_id = uuid.uuid4(), uuid.uuid4()
        with tempfile.TemporaryDirectory() as temporary:
            project_path = Path(temporary) / 'project'
            stage_path = project_path / 'stages' / 'broken-stage'
            stage_path.mkdir(parents=True)
            (stage_path / 'stage.json').write_text(
                f'{{"id":{{"type":"UUID","value":"{stage_id}"}},'
                f'"name":{{"type":"str","value":"Recovered name"}},'
                f'"parent":{{"type":"UUID","value":"{parent_id}"}},'
            )
            project = ProjectDirectory(name='project')
            with patch.object(ProjectDirectory, 'directory_path_for_name', return_value=str(project_path)):
                self.assertEqual(project.stages, [])
                recovered, = project._broken_stages

        self.assertEqual(recovered.id, stage_id)
        self.assertEqual(recovered.parent, parent_id)
        self.assertEqual(recovered.name, 'Recovered name')
        self.assertEqual(project.stages_tree()[0].children[0].value, recovered)


if __name__ == '__main__':
    unittest.main()
