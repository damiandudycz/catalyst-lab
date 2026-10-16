"""Round-trip all typed values persisted in project stage argument files."""
from pathlib import Path
import sys
import types
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.project_stage_argument_serialization import ProjectStageArgumentSerialization as Serialization
from catalystlab.snapshot import PortageProfile
from catalystlab.project_stage_compression_mode import StageCompressionMode
from catalystlab.project_stage_automatic_option import StageAutomaticOption
from catalystlab.project_stage_portage_confdir import StagePortageConfdirSource


class StageArgumentSerializationTests(unittest.TestCase):
    def test_round_trips_supported_scalar_types(self):
        values = [
            'text value', uuid.uuid4(), PortageProfile('default/linux/arm64', 'stable', 'gentoo'),
            StageCompressionMode.xz, StageAutomaticOption.INHERIT_FROM_PARENT,
            StagePortageConfdirSource.PARENT_OVERLAY,
        ]
        for value in values:
            with self.subTest(value=value):
                encoded = Serialization.serialize(value)
                self.assertEqual(Serialization.deserialize(encoded), value)

    def test_round_trips_nested_lists_of_typed_values(self):
        values = [
            StagePortageConfdirSource.RELENG,
            StagePortageConfdirSource.STAGE_OVERLAY,
            [StageCompressionMode.xz, StageCompressionMode.gzip],
        ]
        self.assertEqual(Serialization.deserialize(Serialization.serialize(values)), values)

    def test_unknown_types_preserve_value_for_forward_compatibility(self):
        value = {'new_type': ['value', 3]}
        self.assertEqual(Serialization.deserialize({'type': 'FutureOption', 'value': value}), value)

    def test_deserialize_requires_type_and_value_fields(self):
        with self.assertRaises(KeyError):
            Serialization.deserialize({'type': 'str'})
        with self.assertRaises(KeyError):
            Serialization.deserialize({'value': 'missing type'})


if __name__ == '__main__':
    unittest.main()
