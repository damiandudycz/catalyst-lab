#!/bin/bash
# Builds this checkout and runs Catalyst Lab on Linux or macOS (development).
# See README.md for dependencies. Uses your real Catalyst Lab folder and settings.
set -e

ROOT=$(cd "$(dirname "$0")" && pwd)
case "$(uname -s)" in
    Darwin)
        WORK="$ROOT/.build-macos"
        export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
        # Reuse Homebrew GTK bindings; install additional Python packages in a local venv.
        VENV="$WORK/venv"
        if ! "$VENV/bin/python3" -c 'import gi' 2>/dev/null; then
            GTK_PYTHON=""
            for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
                if [ -x "$candidate" ] && "$candidate" -c 'import gi' 2>/dev/null; then
                    GTK_PYTHON="$candidate"
                    break
                fi
            done
            if [ -z "$GTK_PYTHON" ]; then
                echo "Homebrew Python with PyGObject is required. Install it with: brew install pygobject3" >&2
                exit 1
            fi
            "$GTK_PYTHON" -m venv --system-site-packages "$VENV"
        fi
        PYTHON="$VENV/bin/python3"
        if ! "$PYTHON" -c 'import requests' 2>/dev/null; then
            "$PYTHON" -m pip install --quiet requests
        fi
        # Virtualization.framework runs in the app process. Sign only a private copy
        # of Python, never Homebrew's executable or the venv's symlink to it.
        SOURCE_PYTHON=$("$PYTHON" -c 'import os, sys; p = os.path.join(sys.base_prefix, "Resources/Python.app/Contents/MacOS/Python"); print(p if os.path.isfile(p) else os.path.realpath(sys.executable))')
        VM_PYTHON="$VENV/bin/python3-vm"
        ENTITLEMENTS="$ROOT/src/native/apple-vm.entitlements"
        if [ ! -x "$VM_PYTHON" ] || [ "$SOURCE_PYTHON" -nt "$VM_PYTHON" ] || [ "$ENTITLEMENTS" -nt "$VM_PYTHON" ] || [ "$ROOT/run.sh" -nt "$VM_PYTHON" ] ||
           ! codesign -d --entitlements - "$VM_PYTHON" 2>/dev/null | grep -q com.apple.security.virtualization; then
            cp "$SOURCE_PYTHON" "$VM_PYTHON.tmp"
            codesign --force --sign - --entitlements "$ENTITLEMENTS" "$VM_PYTHON.tmp"
            mv -f "$VM_PYTHON.tmp" "$VM_PYTHON"
        fi
        PYTHON="$VM_PYTHON"
        export PATH="$VENV/bin:$PATH"
        SYSTEM_DATA_DIRS="${HOMEBREW_PREFIX:-/opt/homebrew}/share:/usr/local/share:/usr/share"
        ;;
    Linux)
        WORK="$ROOT/.build-linux"
        PYTHON=$(command -v python3)
        SYSTEM_DATA_DIRS="/usr/local/share:/usr/share"
        ;;
    *)
        echo "Catalyst Lab development launcher supports Linux and macOS only." >&2
        exit 1
        ;;
esac

BUILD="$WORK/build"
PREFIX="$WORK/prefix"
if [ ! -f "$BUILD/build.ninja" ]; then
    meson setup "$BUILD" "$ROOT" --prefix="$PREFIX"
fi
meson install -C "$BUILD" --quiet

export XDG_DATA_DIRS="$PREFIX/share:${XDG_DATA_DIRS:-$SYSTEM_DATA_DIRS}"
export GSETTINGS_SCHEMA_DIR="$PREFIX/share/glib-2.0/schemas"
# Meson caches its Python selection. Explicitly use this platform's interpreter even
# when an existing build directory generated the launcher with a different Python.
exec "$PYTHON" "$PREFIX/bin/catalystlab" "$@"
