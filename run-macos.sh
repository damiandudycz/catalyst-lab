#!/bin/bash
# Builds Catalyst Lab from this checkout and runs it on macOS (development). Requires Homebrew packages:
# brew install gtk4 libadwaita pygobject3 meson ninja squashfs lima
# Uses your real Catalyst Lab folder and settings. Build files are in .build-macos (ignored by git).
set -e
ROOT=$(cd "$(dirname "$0")" && pwd)
BUILD="$ROOT/.build-macos/build"
PREFIX="$ROOT/.build-macos/prefix"
VENV="$ROOT/.build-macos/venv"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# Python environment with Homebrew GTK bindings (pygobject3) and packages missing in Homebrew Python.
if [ ! -x "$VENV/bin/python3" ]; then
    python3 -m venv --system-site-packages "$VENV"
    "$VENV/bin/pip" install --quiet requests
fi
export PATH="$VENV/bin:$PATH" # App launcher uses Python found by meson.

if [ ! -f "$BUILD/build.ninja" ]; then
    meson setup "$BUILD" "$ROOT" --prefix="$PREFIX"
fi
meson install -C "$BUILD" --quiet

export XDG_DATA_DIRS="$PREFIX/share:${HOMEBREW_PREFIX:-/opt/homebrew}/share"
export GSETTINGS_SCHEMA_DIR="$PREFIX/share/glib-2.0/schemas"
exec "$PREFIX/bin/catalystlab" "$@"
