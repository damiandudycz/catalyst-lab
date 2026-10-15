#!/bin/bash
# Installs system-wide Flatpak applications and GNOME Shell extensions in the customized GNOME stage4.
set -euo pipefail

echo "Installing system-wide Flatpak applications and GNOME Shell extensions"

BUS_PID=""
if [ ! -S /run/dbus/system_bus_socket ]; then
    mkdir -p /run/dbus
    dbus-uuidgen --ensure=/etc/machine-id
    BUS_PID=$(dbus-daemon --system --fork --print-pid=1)
fi
cleanup() {
    if [ -n "$BUS_PID" ]; then
        kill "$BUS_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

dconf update

flatpak remote-add --system --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --system --noninteractive --assumeyes flathub \
    io.github.flattool.Warehouse \
    com.mattjakeman.ExtensionManager

shell_version=$(gnome-shell --version | sed -E 's/.* ([0-9]+)(\..*)?$/\1/')
case "$shell_version" in
    ''|*[!0-9]*) echo "Could not determine GNOME Shell version" >&2; exit 1 ;;
esac

tmp_dir=$(mktemp -d)
trap 'rm -rf -- "${tmp_dir:?}"; cleanup' EXIT

install_extension() {
    local uuid="$1"
    local archive="$tmp_dir/$uuid.zip"
    local destination="/usr/share/gnome-shell/extensions/$uuid"
    local metadata="$tmp_dir/$uuid.json"
    local download_url

    echo "Resolving GNOME Shell $shell_version download for $uuid"
    if ! curl --fail --location --silent --show-error \
        --get "https://extensions.gnome.org/extension-info/" \
        --data-urlencode "uuid=$uuid" \
        --data-urlencode "shell_version=$shell_version" \
        --output "$metadata"; then
        echo "Could not fetch GNOME Extensions metadata for $uuid (Shell $shell_version)" >&2
        return 1
    fi
    download_url=$(python3 - "$metadata" "$uuid" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as metadata_file:
    metadata = json.load(metadata_file)
if metadata.get("uuid") != sys.argv[2]:
    raise SystemExit(f"Unexpected GNOME extension UUID: {metadata.get('uuid')!r}")
url = metadata.get("download_url")
if not url:
    raise SystemExit(f"No compatible download for {sys.argv[2]} on this GNOME Shell version")
print(url)
PY
    ) || {
        echo "No usable GNOME Extensions download for $uuid (Shell $shell_version)" >&2
        return 1
    }
    case "$download_url" in
        https://extensions.gnome.org/*) ;;
        /*) download_url="https://extensions.gnome.org$download_url" ;;
        *) echo "Unexpected download URL for $uuid: $download_url" >&2; return 1 ;;
    esac
    if ! curl --fail --location --silent --show-error "$download_url" --output "$archive"; then
        echo "Could not download $uuid for GNOME Shell $shell_version from $download_url" >&2
        return 1
    fi
    mkdir -p "$destination"
    unzip -q "$archive" -d "$destination"
    python3 - "$destination/metadata.json" "$uuid" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as metadata_file:
    metadata = json.load(metadata_file)
if metadata.get("uuid") != sys.argv[2]:
    raise SystemExit(f"Unexpected GNOME extension UUID in {sys.argv[1]}")
PY
    chown -R root:root "$destination"
    chmod -R a+rX,u+w "$destination"
}

install_extension blur-my-shell@aunetx
install_extension dash-to-dock@micxgx.gmail.com
install_extension focus@scaryrawr.github.io
install_extension paperwm@paperwm.github.com

echo "Installed GNOME extras for Shell $shell_version"
