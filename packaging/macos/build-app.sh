#!/bin/bash
# Builds self-contained Catalyst Lab application for macOS: dist/Catalyst Lab.app and dist/Catalyst Lab.dmg.
# Bundle contains Python, GTK, libadwaita, app icons and schemas, squashfs tools and Lima (virtual machines), so it
# runs on Macs without Homebrew. Dependencies are downloaded here:
# - GTK stack, Python and squashfs tools from Homebrew. Packages missing on this Mac are installed only for the build
#   and removed when it ends (with dependencies they brought), unless --keep-packages is given,
# - PyInstaller and Python packages from PyPI (into build environment),
# - Lima release from GitHub (checksum verified).
# Bundle is built for architecture of this Mac.
#
# Usage: packaging/macos/build-app.sh [--no-dmg] [--clean] [--keep-packages]
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HERE="$ROOT/packaging/macos"
WORK="$ROOT/.build-macos-app"
DIST="$ROOT/dist"
APP_NAME="Catalyst Lab"
LIMA_VERSION=${LIMA_VERSION:-2.2.1}
PYTHON_FORMULA=python@3.14 # Same Python that Homebrew pygobject3 is built for.
BREW_PACKAGES=(gtk4 libadwaita pygobject3 adwaita-icon-theme librsvg meson ninja squashfs "$PYTHON_FORMULA")

MAKE_DMG=0
KEEP_PACKAGES=0
for argument in "$@"; do
    case "$argument" in
        --no-dmg) MAKE_DMG=0 ;;
        --clean) rm -rf "$WORK" ;;
        --keep-packages) KEEP_PACKAGES=1 ;;
        *) echo "Unknown option: $argument"; exit 1 ;;
    esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[ "$(uname -s)" = Darwin ] || { echo "This script builds macOS application, run it on macOS."; exit 1; }
case "$(uname -m)" in
    arm64) LIMA_ARCH=arm64 ;;
    x86_64) LIMA_ARCH=x86_64 ;;
    *) echo "Unsupported architecture: $(uname -m)"; exit 1 ;;
esac

# ------------------------------------------------------------------------------
step "Homebrew packages"
BREW=$(command -v brew || true)
[ -n "$BREW" ] || for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$candidate" ] && BREW=$candidate; done
[ -n "$BREW" ] || { echo "Homebrew is required to build the app: https://brew.sh"; exit 1; }
# Homebrew 7 asks for confirmation before installing by default, packages are installed without asking.
export HOMEBREW_NO_ASK=1
BREW_PREFIX=$("$BREW" --prefix)
export PATH="$BREW_PREFIX/bin:$PATH"
missing=()
for package in "${BREW_PACKAGES[@]}"; do
    "$BREW" list --versions "$package" > /dev/null 2>&1 || missing+=("$package")
done
# Formulae installed before build, everything else is removed when build ends (also after failure).
FORMULAE_BEFORE="$WORK/formulae-before"
mkdir -p "$WORK"
"$BREW" list --formula -1 | sort > "$FORMULAE_BEFORE"
remove_build_packages() {
    [ $KEEP_PACKAGES = 0 ] || return 0
    local added
    added=$("$BREW" list --formula -1 | sort | comm -13 "$FORMULAE_BEFORE" -)
    [ -n "$added" ] || return 0
    step "Removing Homebrew packages installed for build"
    echo $added
    # Whole set installed by build is removed, so dependencies between its packages don't matter.
    "$BREW" uninstall --ignore-dependencies $added || echo "Warning: failed to remove some packages, remove them with brew uninstall"
}
if [ ${#missing[@]} -gt 0 ]; then
    trap remove_build_packages EXIT
    echo "Installing ${missing[*]} for build$([ $KEEP_PACKAGES = 0 ] && echo ", removed when it ends")"
    # Packages already installed on this Mac are not rebuilt or reinstalled because of new ones.
    HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1 HOMEBREW_NO_INSTALL_CLEANUP=1 "$BREW" install "${missing[@]}"
else
    echo "All packages are installed"
fi
PYTHON="$("$BREW" --prefix "$PYTHON_FORMULA")/bin/python3.${PYTHON_FORMULA#python@3.}"

# ------------------------------------------------------------------------------
step "Python build environment"
VENV="$WORK/venv"
if [ ! -x "$VENV/bin/python3" ] || [ "$(readlink -f "$VENV/bin/python3")" != "$(readlink -f "$PYTHON")" ]; then
    rm -rf "$VENV"
    "$PYTHON" -m venv --system-site-packages "$VENV" # Homebrew pygobject3 and pycairo.
fi
"$VENV/bin/pip" install --quiet --upgrade "pyinstaller>=6.11,<7" "pyinstaller-hooks-contrib" "requests"
"$VENV/bin/python3" -c "import gi, cairo, requests, PyInstaller; print('PyInstaller', PyInstaller.__version__)"
export PATH="$VENV/bin:$PATH" # Meson uses this Python.

# ------------------------------------------------------------------------------
step "Building Catalyst Lab"
STAGING="$WORK/staging"
rm -rf "$STAGING"
if [ ! -f "$WORK/meson/build.ninja" ]; then
    meson setup "$WORK/meson" "$ROOT" --prefix="$STAGING"
else
    meson configure "$WORK/meson" --prefix="$STAGING" > /dev/null
fi
meson install -C "$WORK/meson" --quiet
VERSION=$(meson introspect --projectinfo "$WORK/meson" | "$VENV/bin/python3" -c "import json, sys; print(json.load(sys.stdin)['version'])")
echo "$VERSION" > "$STAGING/share/catalystlab/VERSION"
echo "Version $VERSION"

# Settings schemas of GTK (file chooser...) and app, compiled together.
SCHEMAS="$WORK/schemas"
rm -rf "$SCHEMAS" && mkdir -p "$SCHEMAS"
cp "$BREW_PREFIX"/share/glib-2.0/schemas/org.gtk.gtk4.*.gschema.xml "$SCHEMAS"/ 2>/dev/null || true
cp "$STAGING"/share/glib-2.0/schemas/*.gschema.xml "$SCHEMAS"/
glib-compile-schemas "$SCHEMAS"

# ------------------------------------------------------------------------------
step "Application icon"
ICONSET="$WORK/CatalystLab.iconset"
ICON="$WORK/CatalystLab.icns"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
SVG="$ROOT/data/icons/hicolor/scalable/apps/com.damiandudycz.CatalystLab.svg"
for size in 16 32 128 256 512; do
    for scale in 1 2; do
        pixels=$((size * scale))
        suffix=""; [ $scale = 2 ] && suffix="@2x"
        name="icon_${size}x${size}${suffix}.png"
        # Artwork takes about 80% of the canvas, like other macOS icons.
        artwork=$((pixels * 13 / 16))
        rsvg-convert -w "$artwork" -h "$artwork" "$SVG" -o "$ICONSET/artwork.png"
        sips --padToHeightWidth "$pixels" "$pixels" "$ICONSET/artwork.png" --out "$ICONSET/$name" > /dev/null
    done
done
rm "$ICONSET/artwork.png"
iconutil -c icns "$ICONSET" -o "$ICON"
echo "$ICON"

# ------------------------------------------------------------------------------
step "Lima $LIMA_VERSION"
LIMA_ARCHIVE="lima-$LIMA_VERSION-Darwin-$LIMA_ARCH.tar.gz"
LIMA_URL="https://github.com/lima-vm/lima/releases/download/v$LIMA_VERSION"
LIMA_DOWNLOADS="$WORK/downloads"
mkdir -p "$LIMA_DOWNLOADS"
if [ ! -f "$LIMA_DOWNLOADS/$LIMA_ARCHIVE" ]; then
    curl -fL --progress-bar -o "$LIMA_DOWNLOADS/$LIMA_ARCHIVE.part" "$LIMA_URL/$LIMA_ARCHIVE"
    mv "$LIMA_DOWNLOADS/$LIMA_ARCHIVE.part" "$LIMA_DOWNLOADS/$LIMA_ARCHIVE"
fi
curl -fsSL -o "$LIMA_DOWNLOADS/SHA256SUMS-$LIMA_VERSION" "$LIMA_URL/SHA256SUMS"
( cd "$LIMA_DOWNLOADS" && grep " $LIMA_ARCHIVE\$" "SHA256SUMS-$LIMA_VERSION" | sed 's/ \*/  /' | shasum -a 256 -c - ) \
    || { echo "Checksum of $LIMA_ARCHIVE doesn't match"; rm -f "$LIMA_DOWNLOADS/$LIMA_ARCHIVE"; exit 1; }
LIMA="$WORK/lima"
rm -rf "$LIMA" && mkdir -p "$LIMA"
tar -xzf "$LIMA_DOWNLOADS/$LIMA_ARCHIVE" -C "$LIMA" bin/limactl share/lima
"$LIMA/bin/limactl" --version

# ------------------------------------------------------------------------------
step "Bundling application"
# PyInstaller cache is cleaned, it could keep compiled modules of previous build.
TOOLS="$(readlink -f "$("$BREW" --prefix squashfs)/bin/unsquashfs"):$(readlink -f "$("$BREW" --prefix squashfs)/bin/mksquashfs")"
CATALYSTLAB_STAGING="$STAGING" CATALYSTLAB_SCHEMAS="$SCHEMAS" CATALYSTLAB_ICON="$ICON" CATALYSTLAB_VERSION="$VERSION" \
CATALYSTLAB_TOOLS="$TOOLS" \
    "$VENV/bin/pyinstaller" --noconfirm --clean --log-level WARN --workpath "$WORK/pyinstaller" --distpath "$WORK/dist" \
    "$HERE/catalystlab.spec"
APP="$WORK/dist/$APP_NAME.app"

# Lima keeps its own signature, which has entitlement for Virtualization framework.
cp -R "$LIMA" "$APP/Contents/Resources/lima"
codesign --force --sign - "$APP" # Seals bundle again after adding Lima (ad-hoc signature).
codesign --verify --strict "$APP"
codesign -d --entitlements - "$APP/Contents/Resources/lima/bin/limactl" 2>/dev/null | grep -q virtualization \
    || echo "Warning: limactl has no virtualization entitlement, virtual machines won't start"

mkdir -p "$DIST"
rm -rf "$DIST/$APP_NAME.app"
ditto "$APP" "$DIST/$APP_NAME.app"
echo "$DIST/$APP_NAME.app ($(du -sh "$DIST/$APP_NAME.app" | cut -f1))"

# ------------------------------------------------------------------------------
if [ $MAKE_DMG = 1 ]; then
    step "Disk image"
    DMG_SOURCE="$WORK/dmg"
    rm -rf "$DMG_SOURCE" && mkdir -p "$DMG_SOURCE"
    ditto "$APP" "$DMG_SOURCE/$APP_NAME.app"
    ln -s /Applications "$DMG_SOURCE/Applications"
    rm -f "$DIST/$APP_NAME.dmg"
    hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$DMG_SOURCE" -ov -format UDZO "$DIST/$APP_NAME.dmg"
    echo "$DIST/$APP_NAME.dmg ($(du -sh "$DIST/$APP_NAME.dmg" | cut -f1))"
fi
