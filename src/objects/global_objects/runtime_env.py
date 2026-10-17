from __future__ import annotations
import os
from enum import Enum, auto
from typing import final

@final
class RuntimeEnv(Enum):
    """Describes different runtime environment types and provides some static environment variables."""
    HOST    = auto()
    FLATPAK = auto()

    @staticmethod
    def current() -> RuntimeEnv:
        """Returns the current runtime environment (FLATPAK or HOST)."""
        return RuntimeEnv.FLATPAK if os.environ.get("FLATPAK_ID") else RuntimeEnv.HOST

    def resolve_path_for_host_access(self, path: str) -> str:
        """Use Flatpak's host bridge when a path exists only on the host."""
        if self is RuntimeEnv.HOST:
            return path

        first_part = path.strip("/").split("/", 1)[0]
        host_candidate = os.path.join("/run/host", first_part)
        if os.path.exists(host_candidate) and not os.path.exists(path):
            return os.path.join("/run/host", path.lstrip("/"))
        return path

