from typing import Any
import uuid
from .snapshot import PortageProfile
from .project_stage_compression_mode import StageCompressionMode
from .project_stage_automatic_option import StageAutomaticOption
from .project_stage_portage_confdir import StagePortageConfdirSource

_ENUM_TYPES = {cls.__name__: cls for cls in
               (StageCompressionMode, StageAutomaticOption, StagePortageConfdirSource)}

class ProjectStageArgumentSerialization:
    """Serialization of arguments for stages json. Stores type name and value."""

    @staticmethod
    def deserialize(value: dict) -> Any:
        data = value['value']
        value_type = value['type']
        if value_type == 'UUID':
            return uuid.UUID(data)
        if value_type == 'PortageProfile':
            return PortageProfile.init_from(data)
        if value_type == 'list':
            return [ProjectStageArgumentSerialization.deserialize(item) for item in data]
        if value_type in _ENUM_TYPES:
            return _ENUM_TYPES[value_type][data]
        return data

    @staticmethod
    def serialize(value) -> dict:
        value_type = type(value)
        if value_type is list:
            value = [ProjectStageArgumentSerialization.serialize(item) for item in value]
        elif value_type is uuid.UUID:
            value = str(value)
        elif value_type is PortageProfile:
            value = value.serialize()
        elif value_type in _ENUM_TYPES.values():
            value = value.name
        return {'type': value_type.__name__, 'value': value}

