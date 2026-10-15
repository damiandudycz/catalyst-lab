"""Version of running Catalyst Lab, set when app starts (from meson project version)."""

APP_VERSION = "0.0.0"

def set_app_version(version: str):
    global APP_VERSION
    APP_VERSION = version

def version_tuple(version: str) -> tuple[int, ...]:
    """Comparable version, eg. (0, 1, 0) for 0.1.0. Parts that are not numbers end it (0.2.0-beta -> (0, 2, 0))."""
    parts = []
    for part in version.split("."):
        digits = ""
        for character in part:
            if not character.isdigit():
                break
            digits += character
        if not digits:
            break
        parts.append(int(digits))
        if len(digits) != len(part):
            break
    return tuple(parts)
