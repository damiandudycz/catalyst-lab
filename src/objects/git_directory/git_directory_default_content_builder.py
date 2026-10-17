from abc import ABC, abstractmethod


class DefaultDirContentBuilder(ABC):
    """Build default content for directories such as Portage overlays."""

    @abstractmethod
    def build_in(self, path: str, data):
        """Create default files in the given directory."""
        ...
