"""Version of running Catalyst Lab, set when app starts (from meson project version)."""
import re

APP_VERSION = "0.0.0"

def set_app_version(version: str):
    global APP_VERSION
    APP_VERSION = version

def version_tuple(version: str) -> tuple[int, ...]:
    """Comparable version, eg. (0, 1, 0) for 0.1.0. Parts that are not numbers end it (0.2.0-beta -> (0, 2, 0))."""
    match = re.match(r"\d+(?:\.\d+)*", version)
    return tuple(map(int, match.group().split("."))) if match else ()
