from abc import ABC, abstractmethod

class DefaultDirContentBuilder(ABC):
    """Base class responsible for generating default content for various"""
    """directories, like portage overlay and similar."""
    @abstractmethod
    def build_in(self, path: str, data):
        """Create default files in given path."""
        pass
