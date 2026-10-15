from enum import Enum, auto

class StageAutomaticOption(Enum):
    INHERIT_FROM_PARENT = auto() # Inherit value from parent stage if selected
    INHERIT_FROM_RELENG_TEMPLATE = auto() # Inherit value from releng_template if selected
    GENERATE_AUTOMATICALLY = auto() # Determine the value dynamically using CatalystLab logic. For example for 'interpreter'
