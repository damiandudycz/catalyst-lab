from typing import Any
import uuid
from .snapshot import PortageProfile
from .project_stage_compression_mode import StageCompressionMode
from .project_stage_automatic_option import StageAutomaticOption
from .project_stage_portage_confdir import StagePortageConfdirSource

class ProjectStageArgumentSerialization:
    """Serialization of arguments for stages json. Stores type name and value."""

    @staticmethod
    def deserialize(value: dict) -> Any:
        t = value['type']
        v = value['value']
        match t:
            case 'str':
                return v
            case 'UUID':
                return uuid.UUID(v)
            case 'PortageProfile':
                return PortageProfile.init_from(v)
            case 'StageCompressionMode':
                return StageCompressionMode[v]
            case 'StageAutomaticOption':
                return StageAutomaticOption[v]
            case 'StagePortageConfdirSource':
                return StagePortageConfdirSource[v]
            case 'list':
                return [ProjectStageArgumentSerialization.deserialize(b) for b in v]
            case _:
                return v

    @staticmethod
    def serialize(value) -> dict:
        t = type(value)
        match t:
            case t if t is uuid.UUID:
                v = str(value)
            case t if t is PortageProfile:
                v = value.serialize()
            case t if t is StageCompressionMode:
                v = value.name
            case t if t is StageAutomaticOption:
                v = value.name
            case t if t is StagePortageConfdirSource:
                v = value.name
            case t if t is list:
                v = [ProjectStageArgumentSerialization.serialize(b) for b in value]
            case _:
                v = value
        return {
            'type': t.__name__,
            'value': v
        }

