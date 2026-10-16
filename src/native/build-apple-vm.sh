#!/bin/sh
set -eu
xcrun swiftc -emit-library -O -module-cache-path "$(dirname "$3")/swift-module-cache" -target "$(uname -m)-apple-macos13.0" "$1" -o "$3"
codesign --force --sign - --entitlements "$2" "$3"
