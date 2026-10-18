"""Keep PyInstaller's GI hook compatible with the packaged PyGObject version."""
from importlib.util import find_spec


# Recent PyGObject releases no longer ship gi._option, although older PyInstaller
# hooks list it unconditionally. Include only helpers present in this environment.
hiddenimports = [
    f"gi.{name}"
    for name in ("_error", "_option", "_enum")
    if find_spec(f"gi.{name}") is not None
]

# PyGObject imports asyncio dynamically on current releases. Collecting it here
# is harmless for older releases and avoids relying on static module analysis.
if find_spec("asyncio") is not None:
    hiddenimports.append("asyncio")
