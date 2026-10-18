#!/bin/bash
set -e

# Use this script to install directly on host without Flatpak.

BUILD_DIR=".build"
INSTALL_MANIFEST="/usr/local/share/catalystlab/.direct-install-manifest.json"
MANIFEST_TEMP="$(mktemp)"
trap 'rm -f "$MANIFEST_TEMP"' EXIT

if [ ! -d "$BUILD_DIR" ]; then
	meson setup "$BUILD_DIR" --prefix=/usr/local
else
	meson configure "$BUILD_DIR" --prefix=/usr/local
fi

ninja -C "$BUILD_DIR"
sudo ninja -C "$BUILD_DIR" install

# Keep a root-owned record of Meson's exact install destinations so uninstall.sh
# removes only files installed by this script, even though the build directory is
# cleaned up below.
meson introspect --installed "$BUILD_DIR" > "$MANIFEST_TEMP"
sudo install -D -m 0644 "$MANIFEST_TEMP" "$INSTALL_MANIFEST"

rm -rf "$BUILD_DIR"
