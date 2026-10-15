#!/bin/bash
# Regenerates dependencies/python3-requests.json with newest versions of Python packages used by the app, using
# flatpak-pip-generator.py (from github.com/flatpak/flatpak-builder-tools) in temporary virtual environment.
#
# Usage: packaging/flatpak/update-python-dependencies.sh
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
PACKAGES=(requests) # Python packages imported by app, which are not in GNOME runtime.
VENV=$(mktemp -d)
trap 'rm -rf "$VENV"' EXIT

python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --disable-pip-version-check "requirements-parser<1.0.0,>=0.11.0" "packaging>=23.0"
cd "$HERE/dependencies"
"$VENV/bin/python3" -I "$HERE/flatpak-pip-generator.py" "${PACKAGES[@]}" --output "python3-${PACKAGES[0]}"
