#!/bin/bash
set -euo pipefail

# Removes a direct host installation made by install.sh. Package-manager installs
# should be removed with their package manager instead.
INSTALL_MANIFEST="/usr/local/share/catalystlab/.direct-install-manifest.json"

if [[ ! -f "$INSTALL_MANIFEST" ]]; then
	echo "No install record found at $INSTALL_MANIFEST." >&2
	echo "This installation may predate install.sh tracking; reinstall with ./install.sh, then run ./uninstall.sh." >&2
	exit 1
fi

installed_file_list="$(python3 - "$INSTALL_MANIFEST" <<'PY'
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as manifest_file:
    installed = json.load(manifest_file)

prefix = "/usr/local"
for path in sorted(set(installed.values())):
    normalized = os.path.normpath(path)
    if not os.path.isabs(normalized) or os.path.commonpath((prefix, normalized)) != prefix or normalized == prefix:
        raise SystemExit(f"Refusing to remove path outside the direct-install prefix: {path}")
    print(normalized)
PY
)"
installed_files=()
if [[ -n "$installed_file_list" ]]; then
	mapfile -t installed_files <<< "$installed_file_list"
fi

if (( ${#installed_files[@]} == 0 )); then
	echo "The install record contains no files; nothing to uninstall."
	exit 0
fi

sudo rm -f -- "${installed_files[@]}"

# Refresh shared caches affected by the installed application files.
schema_dirs=()
icon_dirs=()
desktop_dirs=()
for path in "${installed_files[@]}"; do
	case "$path" in
		*/glib-2.0/schemas/*) schema_dirs+=("${path%/*}") ;;
		*/icons/hicolor/*) icon_dirs+=("${path%%/icons/hicolor/*}/icons/hicolor") ;;
		*/applications/*) desktop_dirs+=("${path%/*}") ;;
	esac
done

mapfile -t schema_dirs < <(printf '%s\n' "${schema_dirs[@]}" | sort -u)
mapfile -t icon_dirs < <(printf '%s\n' "${icon_dirs[@]}" | sort -u)
mapfile -t desktop_dirs < <(printf '%s\n' "${desktop_dirs[@]}" | sort -u)

if ((${#schema_dirs[@]})); then
	for directory in "${schema_dirs[@]}"; do
		sudo glib-compile-schemas "$directory"
	done
fi
if ((${#icon_dirs[@]})); then
	for directory in "${icon_dirs[@]}"; do
		sudo gtk-update-icon-cache --force --quiet "$directory"
	done
fi
if ((${#desktop_dirs[@]})) && command -v update-desktop-database >/dev/null; then
	for directory in "${desktop_dirs[@]}"; do
		sudo update-desktop-database --quiet "$directory"
	done
fi

sudo rm -f "$INSTALL_MANIFEST"
echo "Catalyst Lab has been uninstalled. User data in your home directory was left untouched."
