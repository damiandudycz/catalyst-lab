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
        DEV_APP="$VENV/Catalyst Lab.app"
        VM_PYTHON="$VENV/bin/python3-vm"
        APP_PYTHON="$DEV_APP/Contents/MacOS/python3-vm"
        ENTITLEMENTS="$ROOT/src/native/apple-vm.entitlements"
        if [ ! -x "$APP_PYTHON" ] || [ "$SOURCE_PYTHON" -nt "$APP_PYTHON" ] || [ "$ENTITLEMENTS" -nt "$APP_PYTHON" ] || [ "$ROOT/run.sh" -nt "$APP_PYTHON" ]; then
            mkdir -p "$DEV_APP/Contents/MacOS"
            cp "$SOURCE_PYTHON" "$APP_PYTHON"
            "$PYTHON" - "$DEV_APP/Contents/Info.plist" <<'PYPLIST'
import plistlib, sys
with open(sys.argv[1], 'wb') as stream:
    plistlib.dump({
        'CFBundleIdentifier': 'com.damiandudycz.CatalystLab.Development',
        'CFBundleName': 'Catalyst Lab Development',
        'CFBundleExecutable': 'python3-vm',
        'CFBundlePackageType': 'APPL',
        'NSLocationWhenInUseUsageDescription': 'Catalyst Lab uses Location access to suggest your connected Wi-Fi network when deploying a system.',
        'NSLocationUsageDescription': 'Catalyst Lab uses Location access to suggest your connected Wi-Fi network when deploying a system.',
    }, stream)
PYPLIST
            codesign --force --sign - --identifier com.damiandudycz.CatalystLab.Development --entitlements "$ENTITLEMENTS" "$DEV_APP"
            rm -f "$VM_PYTHON"
            ln -s "$APP_PYTHON" "$VM_PYTHON"
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

# Keep the development launcher isolated from an installed Catalyst Lab instance.
export CATALYSTLAB_APPLICATION_ID="com.damiandudycz.CatalystLab.Development"
# Enable private plain-text chat capture for this development launcher only.
export CATALYSTLAB_DEBUG_CONVERSATION_LOGS=1
# Use this checkout's branch for official template checks and downloads.
TEMPLATE_BRANCH=$(git -C "$ROOT" branch --show-current)
if [ -n "$TEMPLATE_BRANCH" ]; then
    export CATALYSTLAB_TEMPLATE_BRANCH="$TEMPLATE_BRANCH"
fi

BUILD="$WORK/build"
PREFIX="$WORK/prefix"
if [ ! -f "$BUILD/build.ninja" ]; then
    meson setup "$BUILD" "$ROOT" --prefix="$PREFIX"
else
    # Source and install lists change as the application grows; refreshing the
    # build graph prevents an old Ninja file from silently skipping new modules.
    meson setup --reconfigure "$BUILD" "$ROOT" --prefix="$PREFIX"
fi
meson compile -C "$BUILD"
# Meson installs changed files but leaves removed Python modules behind, and the
# module scanner imports every file in this package. Keep the dev install in sync.
PYTHON_PACKAGE="$PREFIX/share/catalystlab/catalystlab"
if [ -d "$PYTHON_PACKAGE" ]; then
    find "$PYTHON_PACKAGE" -type f -name '*.py' -delete
fi
meson install -C "$BUILD" --quiet

export XDG_DATA_DIRS="$PREFIX/share:${XDG_DATA_DIRS:-$SYSTEM_DATA_DIRS}"
export GSETTINGS_SCHEMA_DIR="$PREFIX/share/glib-2.0/schemas"
# Meson caches its Python selection. Explicitly use this platform's interpreter even
# when an existing build directory generated the launcher with a different Python.
exec "$PYTHON" "$PREFIX/bin/catalystlab" "$@"
