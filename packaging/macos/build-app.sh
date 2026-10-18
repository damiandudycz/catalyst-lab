#!/bin/bash
# Builds self-contained Catalyst Lab application for macOS: dist/Catalyst Lab.app and dist/Catalyst Lab.dmg.
# Bundle contains Python, GTK, libadwaita, app icons and schemas, squashfs tools and Lima (virtual machines), so it
# runs on Macs without Homebrew. Build dependencies are installed in a private Conda-forge environment under
# .build-macos-app; GTK and libadwaita are built from source into private prefixes and never replace system copies.
# Other dependencies are downloaded into that environment, while Lima is fetched from GitHub (checksum verified).
# Bundle is built for architecture of this Mac.
#
# Usage: packaging/macos/build-app.sh [--no-dmg] [--clean]
# GTK_REF and LIBADWAITA_REF can override the source refs (defaults: main and 1.10.0).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HERE="$ROOT/packaging/macos"
WORK="$ROOT/.build-macos-app"
DIST="$ROOT/dist"
APP_NAME="Catalyst Lab"
LIMA_VERSION=${LIMA_VERSION:-2.2.1}
GTK_REF=${GTK_REF:-main}
LIBADWAITA_REF=${LIBADWAITA_REF:-1.10.0}

MAKE_DMG=0
for argument in "$@"; do
    case "$argument" in
        --no-dmg) MAKE_DMG=0 ;;
        --clean) rm -rf "$WORK" ;;
        *) echo "Unknown option: $argument"; exit 1 ;;
    esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[ "$(uname -s)" = Darwin ] || { echo "This script builds macOS application, run it on macOS."; exit 1; }
xcrun --find clang >/dev/null 2>&1 || {
    echo "Xcode Command Line Tools are required to build GTK and libadwaita; install them with 'xcode-select --install'."
    exit 1
}
case "$(uname -m)" in
    arm64) LIMA_ARCH=arm64 ;;
    x86_64) LIMA_ARCH=x86_64 ;;
    *) echo "Unsupported architecture: $(uname -m)"; exit 1 ;;
esac

# ------------------------------------------------------------------------------
step "Preparing isolated Conda-forge build environment"
mkdir -p "$WORK/tools" "$WORK/downloads" "$WORK/sources" "$WORK/private"
case "$(uname -m)" in
    arm64) MAMBA_PLATFORM=osx-arm64 ;;
    x86_64) MAMBA_PLATFORM=osx-64 ;;
esac
MICROMAMBA="$WORK/tools/micromamba"
if [ ! -x "$MICROMAMBA" ]; then
    MAMBA_ARCHIVE="$WORK/downloads/micromamba.tar.bz2"
    curl -fL --retry 3 "https://micro.mamba.pm/api/micromamba/$MAMBA_PLATFORM/latest" -o "$MAMBA_ARCHIVE"
    tar -xjf "$MAMBA_ARCHIVE" -C "$WORK/tools" bin/micromamba
    mv "$WORK/tools/bin/micromamba" "$MICROMAMBA"
    rmdir "$WORK/tools/bin"
fi
ENV="$WORK/env"
MAMBA_ROOT_PREFIX="$WORK/mamba"
export MAMBA_ROOT_PREFIX
MAMBA_PACKAGES=(
    python=3.14 pygobject pycairo gobject-introspection glib pango cairo harfbuzz fribidi \
    gdk-pixbuf graphene epoxy libtiff libpng libjpeg-turbo zlib \
    meson ninja pkg-config gettext librsvg glib-networking adwaita-icon-theme squashfs-tools \
    pyinstaller pyinstaller-hooks-contrib requests
)
if [ -d "$ENV/conda-meta" ]; then
    "$MICROMAMBA" install --yes --prefix "$ENV" --channel conda-forge --override-channels \
        --strict-channel-priority "${MAMBA_PACKAGES[@]}"
else
    "$MICROMAMBA" create --yes --prefix "$ENV" --channel conda-forge --override-channels \
        --strict-channel-priority "${MAMBA_PACKAGES[@]}"
fi
PYTHON="$ENV/bin/python3"
export PATH="$ENV/bin:/usr/bin:/bin:/usr/sbin:/sbin"
unset PYTHONPATH PYTHONHOME
export PKG_CONFIG_PATH="$ENV/lib/pkgconfig:$ENV/share/pkgconfig"
export GI_TYPELIB_PATH="$ENV/lib/girepository-1.0"
export DYLD_LIBRARY_PATH="$ENV/lib"

# Build GTK and libadwaita from source into private prefixes. GTK's development branch contains the newer macOS
# frame-presentation implementation; all build and runtime dependencies remain under .build-macos-app.
GTK_SOURCE="$WORK/sources/gtk"
GTK_BUILD="$WORK/gtk-build"
GTK_PREFIX="$WORK/private/gtk"
ADWAITA_SOURCE="$WORK/sources/libadwaita"
ADWAITA_BUILD="$WORK/libadwaita-build"
ADWAITA_PREFIX="$WORK/private/libadwaita"
if [ ! -d "$GTK_SOURCE/.git" ]; then
    git clone --depth 1 --branch "$GTK_REF" https://gitlab.gnome.org/GNOME/gtk.git "$GTK_SOURCE"
else
    git -C "$GTK_SOURCE" fetch --depth 1 origin "$GTK_REF"
    git -C "$GTK_SOURCE" checkout --detach FETCH_HEAD
    git -C "$GTK_SOURCE" reset --hard FETCH_HEAD
fi
if [ -f "$GTK_BUILD/build.ninja" ]; then
    meson setup --reconfigure "$GTK_BUILD" "$GTK_SOURCE" --prefix="$GTK_PREFIX" --buildtype=release \
        -Dintrospection=enabled -Dmedia-gstreamer=disabled -Dvulkan=disabled -Dtracker=disabled -Dcolord=disabled \
        -Ddocumentation=false -Dman-pages=false -Dbuild-demos=false -Dbuild-tests=false \
        -Dbuild-testsuite=false -Dbuild-examples=false -Dprint-cups=disabled
else
    meson setup "$GTK_BUILD" "$GTK_SOURCE" --prefix="$GTK_PREFIX" --buildtype=release \
        -Dintrospection=enabled -Dmedia-gstreamer=disabled -Dvulkan=disabled -Dtracker=disabled -Dcolord=disabled \
        -Ddocumentation=false -Dman-pages=false -Dbuild-demos=false -Dbuild-tests=false \
        -Dbuild-testsuite=false -Dbuild-examples=false -Dprint-cups=disabled
fi
meson install -C "$GTK_BUILD" --quiet

# GTK builds its fallback sassc against libsass with a build-tree rpath. libadwaita also uses this installed
# sassc to compile its stylesheets, so add the relocated private GTK lib directory to the executable's runpaths.
if [ -x "$GTK_PREFIX/bin/sassc" ] && [ -f "$GTK_PREFIX/lib/libsass.dylib" ] && \
    ! otool -l "$GTK_PREFIX/bin/sassc" | grep -Fq "path @loader_path/../lib ("; then
    install_name_tool -add_rpath "@loader_path/../lib" "$GTK_PREFIX/bin/sassc"
fi

export PATH="$GTK_PREFIX/bin:$PATH"
export PKG_CONFIG_PATH="$GTK_PREFIX/lib/pkgconfig:$ENV/lib/pkgconfig:$ENV/share/pkgconfig"
export GI_TYPELIB_PATH="$GTK_PREFIX/lib/girepository-1.0:$ENV/lib/girepository-1.0"
export GI_GIR_PATH="$GTK_PREFIX/share/gir-1.0:$ENV/share/gir-1.0"
export DYLD_LIBRARY_PATH="$GTK_PREFIX/lib:$ENV/lib"
if [ ! -d "$ADWAITA_SOURCE/.git" ]; then
    git clone --depth 1 --branch "$LIBADWAITA_REF" https://gitlab.gnome.org/GNOME/libadwaita.git "$ADWAITA_SOURCE"
else
    git -C "$ADWAITA_SOURCE" fetch --depth 1 origin "$LIBADWAITA_REF"
    git -C "$ADWAITA_SOURCE" checkout --detach FETCH_HEAD
    git -C "$ADWAITA_SOURCE" reset --hard FETCH_HEAD
fi
if [ -f "$ADWAITA_BUILD/build.ninja" ]; then
    meson setup --reconfigure "$ADWAITA_BUILD" "$ADWAITA_SOURCE" --prefix="$ADWAITA_PREFIX" --buildtype=release \
        -Dintrospection=enabled -Dvapi=false -Dtests=false -Dexamples=false -Dgtk_doc=false
else
    meson setup "$ADWAITA_BUILD" "$ADWAITA_SOURCE" --prefix="$ADWAITA_PREFIX" --buildtype=release \
        -Dintrospection=enabled -Dvapi=false -Dtests=false -Dexamples=false -Dgtk_doc=false
fi
meson install -C "$ADWAITA_BUILD" --quiet
export PKG_CONFIG_PATH="$ADWAITA_PREFIX/lib/pkgconfig:$GTK_PREFIX/lib/pkgconfig:$ENV/lib/pkgconfig:$ENV/share/pkgconfig"
export GI_TYPELIB_PATH="$ADWAITA_PREFIX/lib/girepository-1.0:$GTK_PREFIX/lib/girepository-1.0:$ENV/lib/girepository-1.0"
export DYLD_LIBRARY_PATH="$ADWAITA_PREFIX/lib:$GTK_PREFIX/lib:$ENV/lib"

echo "Private GTK $(pkg-config --modversion gtk4); libadwaita $(pkg-config --modversion libadwaita-1)"

# PyInstaller's GI hooks locate sibling-prefix GIR files relative to the shared build root. Keep a staging copy there
# so it can rewrite the typelib library paths for the relocated app bundle.
GI_HOOK_GIR="$WORK/share/gir-1.0"
mkdir -p "$GI_HOOK_GIR"
cp "$GTK_PREFIX"/share/gir-1.0/*.gir "$GI_HOOK_GIR"/
cp "$ADWAITA_PREFIX"/share/gir-1.0/*.gir "$GI_HOOK_GIR"/

# ------------------------------------------------------------------------------
step "Checking isolated Python and GTK runtime"
"$PYTHON" -c "import gi, cairo, requests, PyInstaller; gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1'); gi.require_version('GdkMacos', '4.0'); from gi.repository import Gtk, Adw, GdkMacos; print('PyInstaller', PyInstaller.__version__, 'GTK', Gtk.get_major_version(), Gtk.get_minor_version(), 'Adw', Adw.get_major_version(), Adw.get_minor_version())"

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
VERSION=$(meson introspect --projectinfo "$WORK/meson" | "$PYTHON" -c "import json, sys; print(json.load(sys.stdin)['version'])")
echo "$VERSION" > "$STAGING/share/catalystlab/VERSION"
echo "Version $VERSION"

# Settings schemas of GTK (file chooser...) and app, compiled together.
SCHEMAS="$WORK/schemas"
rm -rf "$SCHEMAS" && mkdir -p "$SCHEMAS"
cp "$GTK_PREFIX"/share/glib-2.0/schemas/org.gtk.gtk4.*.gschema.xml "$SCHEMAS"/ 2>/dev/null || true
cp "$STAGING"/share/glib-2.0/schemas/*.gschema.xml "$SCHEMAS"/
"$ENV/bin/glib-compile-schemas" "$SCHEMAS"

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
TOOLS="$ENV/bin/unsquashfs:$ENV/bin/mksquashfs"
CATALYSTLAB_STAGING="$STAGING" CATALYSTLAB_SCHEMAS="$SCHEMAS" CATALYSTLAB_ICON="$ICON" CATALYSTLAB_VERSION="$VERSION" \
CATALYSTLAB_GTK_DATA="$GTK_PREFIX/share/gtk-4.0" \
CATALYSTLAB_ICON_DATA="$ENV/share/icons/Adwaita" \
CATALYSTLAB_GIO_MODULE_DIR="$ENV/lib/gio/modules" \
CATALYSTLAB_TOOLS="$TOOLS" \
    "$ENV/bin/pyinstaller" --noconfirm --clean --log-level WARN --workpath "$WORK/pyinstaller" --distpath "$WORK/dist" \
    "$HERE/catalystlab.spec"
APP="$WORK/dist/$APP_NAME.app"

# Lima keeps its own signature, which has entitlement for Virtualization framework.
cp -R "$LIMA" "$APP/Contents/Resources/lima"
codesign --force --sign - --entitlements "$ROOT/src/native/apple-vm.entitlements" "$APP"
codesign --verify --strict "$APP"
codesign -d --entitlements - "$APP/Contents/Resources/lima/bin/limactl" 2>/dev/null | grep -q virtualization \
    || echo "Warning: limactl has no virtualization entitlement, virtual machines won't start"

mkdir -p "$DIST"
rm -rf "$DIST/$APP_NAME.app"
ditto "$APP" "$DIST/$APP_NAME.app"
codesign --force --sign - --entitlements "$ROOT/src/native/apple-vm.entitlements" "$DIST/$APP_NAME.app"
codesign --verify --deep --strict "$DIST/$APP_NAME.app"
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
