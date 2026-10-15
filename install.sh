#!/bin/bash
set -e

# Use this script to install directly on host without Flatpak.

BUILD_DIR=".build"

if [ ! -d "$BUILD_DIR" ]; then
    meson setup "$BUILD_DIR"
fi

ninja -C "$BUILD_DIR"
sudo ninja -C "$BUILD_DIR" install

rm -rf "$BUILD_DIR"
