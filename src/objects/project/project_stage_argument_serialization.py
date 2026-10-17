from typing import Any
import uuid
from .snapshot import PortageProfile
from .project_stage_compression_mode import StageCompressionMode
from .project_stage_automatic_option import StageAutomaticOption
from .project_stage_portage_confdir import StagePortageConfdirSource

_DESERIALIZERS = {
    'str': lambda value: value,
    'UUID': uuid.UUID,
    'PortageProfile': PortageProfile.init_from,
    'StageCompressionMode': lambda value: StageCompressionMode[value],
    'StageAutomaticOption': lambda value: StageAutomaticOption[value],
    'StagePortageConfdirSource': lambda value: StagePortageConfdirSource[value],
    'list': lambda values: [ProjectStageArgumentSerialization.deserialize(item) for item in values],
}
_SERIALIZERS = {
    uuid.UUID: str,
    PortageProfile: lambda value: value.serialize(),
    StageCompressionMode: lambda value: value.name,
    StageAutomaticOption: lambda value: value.name,
    StagePortageConfdirSource: lambda value: value.name,
}

class ProjectStageArgumentSerialization:
    """Serialization of arguments for stages json. Stores type name and value."""

    @staticmethod
    def deserialize(value: dict) -> Any:
        data = value['value']
        deserializer = _DESERIALIZERS.get(value['type'])
        return deserializer(data) if deserializer else data

    @staticmethod
    def serialize(value) -> dict:
        value_type = type(value)
        serializer = _SERIALIZERS.get(value_type)
        if value_type is list:
            value = [ProjectStageArgumentSerialization.serialize(item) for item in value]
        elif serializer:
            value = serializer(value)
        return {'type': value_type.__name__, 'value': value}

