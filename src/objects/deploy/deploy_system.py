from __future__ import annotations
import glob, ipaddress, os, re, subprocess
from dataclasses import dataclass, field
from enum import Enum
from .ssh_connection import quote

# ------------------------------------------------------------------------------
# Configuration of deployed system: network, localization and SSH keys. Scripts run on machine, with ROOT set to
# root of installed system. Services are enabled for init system of stage (OpenRC or systemd).

class NetworkService(Enum):
    NETWORK_MANAGER = "networkmanager"
    SYSTEMD_NETWORKD = "systemd-networkd"
    DHCPCD = "dhcpcd"
    NETIFRC = "netifrc"
    NONE = "none"

    @property
    def display_name(self) -> str:
        return {
            NetworkService.NETWORK_MANAGER: "NetworkManager",
            NetworkService.SYSTEMD_NETWORKD: "systemd-networkd",
            NetworkService.DHCPCD: "dhcpcd",
            NetworkService.NETIFRC: "netifrc (OpenRC net scripts)",
            NetworkService.NONE: "Don't configure network",
        }[self]

    @property
    def package(self) -> str | None:
        """Package installed when stage doesn't contain service."""
        return {NetworkService.DHCPCD: "net-misc/dhcpcd", NetworkService.NETWORK_MANAGER: "net-misc/networkmanager"}.get(self)

    def supported(self, init: str | None) -> bool:
        if self == NetworkService.SYSTEMD_NETWORKD:
            return init == "systemd"
        if self == NetworkService.NETIFRC:
            return init != "systemd"
        return True

# Files showing that stage contains service.
NETWORK_SERVICE_FILES = {
    NetworkService.NETWORK_MANAGER: re.compile(r"^usr/s?bin/NetworkManager$"),
    NetworkService.SYSTEMD_NETWORKD: re.compile(r"^(usr/)?lib/systemd/systemd-networkd$"),
    NetworkService.DHCPCD: re.compile(r"^(usr/)?s?bin/dhcpcd$"),
    NetworkService.NETIFRC: re.compile(r"^etc/init\.d/net\.lo$"),
}

def default_network_service(available: set[NetworkService], init: str | None) -> NetworkService:
    for service in (NetworkService.NETWORK_MANAGER, NetworkService.SYSTEMD_NETWORKD, NetworkService.DHCPCD, NetworkService.NETIFRC):
        if service in available and service.supported(init):
            return service
    return NetworkService.DHCPCD # Installed from repository.

@dataclass
class NetworkSettings:
    service: NetworkService = NetworkService.NONE
    static: bool = False # Otherwise DHCP.
    interface: str = ""
    address: str = "" # With prefix, eg. 192.168.1.10/24.
    gateway: str = ""
    dns: str = ""

    def error(self) -> str | None:
        if self.service == NetworkService.NONE or not self.static:
            return None
        if not re.match(r"^[\w.-]+$", self.interface):
            return "Set network interface"
        try:
            ipaddress.ip_interface(self.address)
            if "/" not in self.address:
                return "Address needs prefix length, eg. 192.168.1.10/24"
            if self.gateway:
                ipaddress.ip_address(self.gateway)
            for server in self.dns.replace(",", " ").split():
                ipaddress.ip_address(server)
        except ValueError as e:
            return f"Invalid address: {e}"
        return None

    @property
    def dns_servers(self) -> list[str]:
        return self.dns.replace(",", " ").split()

def init_system_script() -> str:
    """Detect the installed system, never the live CD running deployment.

    Read init symlink text without resolving absolute targets against the live CD.
    Compatibility scripts and empty runlevel directories are weaker evidence.
    """
    return r'''CL_INIT_SYSTEM=
for CL_INIT_PATH in usr/bin/init usr/sbin/init sbin/init bin/init; do
    CL_INIT_TARGET=$(readlink "$ROOT/$CL_INIT_PATH" 2>/dev/null || true)
    case "$CL_INIT_TARGET" in
        */systemd|systemd) CL_INIT_SYSTEM=systemd; break ;;
        */openrc-init|openrc-init|*/sysvinit|sysvinit|*/busybox|busybox) CL_INIT_SYSTEM=openrc; break ;;
    esac
done
if [ -z "$CL_INIT_SYSTEM" ]; then
    if [ -e "$ROOT/usr/lib/systemd/systemd" ] || [ -e "$ROOT/lib/systemd/systemd" ]; then
        CL_INIT_SYSTEM=systemd
    elif [ -x "$ROOT/sbin/openrc-run" ] || [ -x "$ROOT/usr/bin/openrc-run" ] || [ -x "$ROOT/usr/sbin/openrc-run" ] || [ -d "$ROOT/etc/runlevels" ]; then
        CL_INIT_SYSTEM=openrc
    elif [ -d "$ROOT/usr/lib/systemd/system" ] || [ -d "$ROOT/lib/systemd/system" ]; then
        CL_INIT_SYSTEM=systemd
    fi
fi'''


def enable_service_script(openrc: str | None, systemd: str | None) -> str:
    """Enable only the service for the target's init system."""
    lines = [init_system_script()]
    if openrc:
        lines += [
            f'if [ "$CL_INIT_SYSTEM" = openrc ] && [ -e "$ROOT/etc/init.d/{openrc}" ]; then',
            '    mkdir -p "$ROOT/etc/runlevels/default"',
            f'    ln -sf /etc/init.d/{openrc} "$ROOT/etc/runlevels/default/{openrc}"',
            f'    echo "Enabled {openrc} (OpenRC)"',
            'fi',
        ]
    if systemd:
        lines += [
            'if [ "$CL_INIT_SYSTEM" = systemd ]; then',
            '    for CL_UNIT_BASE in usr/lib lib; do',
            f'        if [ -e "$ROOT/$CL_UNIT_BASE/systemd/system/{systemd}.service" ]; then',
            '            mkdir -p "$ROOT/etc/systemd/system/multi-user.target.wants"',
            f'            ln -sf "/$CL_UNIT_BASE/systemd/system/{systemd}.service" "$ROOT/etc/systemd/system/multi-user.target.wants/{systemd}.service"',
            f'            echo "Enabled {systemd} (systemd)"',
            '            break',
            '        fi',
            '    done',
            'fi',
        ]
    return "\n".join(lines)


def enable_systemd_instance_script(unit: str, instance: str) -> str:
    """Enable a template unit only in a systemd target."""
    wants = "$ROOT/etc/systemd/system/multi-user.target.wants"
    return init_system_script() + "\n" + (
        f'if [ "$CL_INIT_SYSTEM" = systemd ] && [ -e "$ROOT/usr/lib/systemd/system/{unit}@.service" ]; then mkdir -p "{wants}" && '
        f'ln -sf /usr/lib/systemd/system/{unit}@.service "{wants}/{unit}@{instance}.service" && echo "Enabled {unit}@{instance} (systemd)"; fi')

def network_script(settings: NetworkSettings) -> str:
    service = settings.service
    if service == NetworkService.NONE:
        return ""
    lines = []
    interface = settings.interface
    address = settings.address
    if service == NetworkService.NETWORK_MANAGER:
        lines.append(enable_service_script("NetworkManager", "NetworkManager"))
        if settings.static:
            dns = "".join(f"{server};" for server in settings.dns_servers)
            connection = "\n".join([
                "[connection]", "id=wired", "type=ethernet", f"interface-name={interface}", "",
                "[ipv4]", "method=manual", f"address1={address}" + (f",{settings.gateway}" if settings.gateway else ""),
                *([f"dns={dns}"] if dns else []), "", "[ipv6]", "method=auto", "",
            ])
            lines += [
                'mkdir -p "$ROOT/etc/NetworkManager/system-connections"',
                f'printf "%s" {quote(connection)} > "$ROOT/etc/NetworkManager/system-connections/wired.nmconnection"',
                'chmod 600 "$ROOT/etc/NetworkManager/system-connections/wired.nmconnection"',
            ]
    elif service == NetworkService.SYSTEMD_NETWORKD:
        lines.append(enable_service_script(None, "systemd-networkd"))
        lines.append(enable_service_script(None, "systemd-resolved"))
        network = ["[Match]", f"Name={interface or 'en* eth*'}", "", "[Network]"]
        if settings.static:
            network += [f"Address={address}"] + ([f"Gateway={settings.gateway}"] if settings.gateway else []) + [f"DNS={server}" for server in settings.dns_servers]
        else:
            network += ["DHCP=yes"]
        lines += ['mkdir -p "$ROOT/etc/systemd/network"',
                  f'printf "%s\\n" {quote(chr(10).join(network))} > "$ROOT/etc/systemd/network/20-wired.network"']
    elif service == NetworkService.DHCPCD:
        lines.append(enable_service_script("dhcpcd", "dhcpcd"))
        if settings.static:
            config = [f"interface {interface}", f"static ip_address={address}"]
            config += [f"static routers={settings.gateway}"] if settings.gateway else []
            config += [f"static domain_name_servers={' '.join(settings.dns_servers)}"] if settings.dns_servers else []
            lines.append(f'printf "\\n%s\\n" {quote(chr(10).join(config))} >> "$ROOT/etc/dhcpcd.conf"')
    elif service == NetworkService.NETIFRC:
        name = interface or "eth0"
        if settings.static:
            config = [f'config_{name}="{address}"'] + ([f'routes_{name}="default via {settings.gateway}"'] if settings.gateway else [])
            config += [f'dns_servers_{name}="{" ".join(settings.dns_servers)}"'] if settings.dns_servers else []
        else:
            config = [f'config_{name}="dhcp"']
        lines += [f'printf "%s\\n" {quote(chr(10).join(config))} >> "$ROOT/etc/conf.d/net"',
                  f'ln -sf net.lo "$ROOT/etc/init.d/net.{name}"',
                  f'ln -sf /etc/init.d/net.{name} "$ROOT/etc/runlevels/default/net.{name}" && echo "Enabled net.{name} (OpenRC)"']
    if settings.static and settings.dns_servers:
        # Used until network service manages it.
        lines.append(f'printf "nameserver %s\\n" {" ".join(quote(server) for server in settings.dns_servers)} > "$ROOT/etc/resolv.conf"')
    lines.append(f'echo "Network: {service.display_name}, {"static " + address if settings.static else "DHCP"}"')
    return "\n".join(lines)

# ------------------------------------------------------------------------------
# Localization:

@dataclass
class LocalizationSettings:
    timezone: str = ""  # Eg. Europe/Warsaw.
    locales: list[str] = field(default_factory=list) # Generated locales, eg. pl_PL.UTF-8, first one is system language.
    keymap: str = ""    # Console keymap, eg. us, pl.

    def error(self) -> str | None:
        if self.timezone and not re.match(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*$", self.timezone):
            return "Invalid timezone, use name like Europe/Warsaw"
        for locale in self.locales:
            if not re.match(r"^[A-Za-z_@.0-9\-]+$", locale):
                return f"Invalid locale {locale}, use name like en_US.UTF-8"
        if len(set(self.locales)) != len(self.locales):
            return "Locales must be different"
        if self.keymap and not re.match(r"^[A-Za-z0-9_\-]+$", self.keymap):
            return "Invalid keyboard layout, use name like us"
        return None

def localization_script(settings: LocalizationSettings) -> str:
    lines = []
    if settings.timezone:
        timezone = quote(settings.timezone)
        lines += [
            f'if [ -e "$ROOT/usr/share/zoneinfo/"{timezone} ]; then',
            f'    ln -sf ../usr/share/zoneinfo/{settings.timezone} "$ROOT/etc/localtime"; echo {timezone} > "$ROOT/etc/timezone"; echo "Timezone: {settings.timezone}"',
            f'else echo "Warning: timezone {settings.timezone} doesn\'t exist in stage"; fi',
        ]
    if settings.locales:
        # glibc locales are generated, musl has no locale-gen. C and POSIX locales are built in.
        lines.append('if [ -x "$ROOT/usr/sbin/locale-gen" ]; then')
        for locale in settings.locales:
            if locale.split(".")[0] in ("C", "POSIX"):
                continue
            charset = locale.split(".", 1)[1] if "." in locale else "UTF-8"
            lines.append(f'    grep -q "^{locale} " "$ROOT/etc/locale.gen" 2>/dev/null || echo "{locale} {charset}" >> "$ROOT/etc/locale.gen"')
        lines += ['    chroot "$ROOT" locale-gen', 'fi']
        language = settings.locales[0]
        lines += [
            f'echo "LANG=\\"{language}\\"" > "$ROOT/etc/env.d/02locale"',
            f'echo "LANG={language}" > "$ROOT/etc/locale.conf"',
            f'echo "Locales: {", ".join(settings.locales)}, system language {language}"',
        ]
    if settings.keymap:
        keymap = settings.keymap
        lines += [
            f'[ -f "$ROOT/etc/conf.d/keymaps" ] && sed -i "s/^keymap=.*/keymap=\\"{keymap}\\"/" "$ROOT/etc/conf.d/keymaps"',
            f'echo "KEYMAP={keymap}" > "$ROOT/etc/vconsole.conf"',
            f'echo "Keyboard layout: {keymap}"',
        ]
    return "\n".join(lines)

def local_localization() -> LocalizationSettings:
    """Localization of this computer, suggested for deployed system."""
    timezone = ""
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            timezone = target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    locale = ""
    language = os.environ.get("LANG", "")
    if re.match(r"^[a-z]{2,3}_[A-Z]{2}", language):
        locale = language.split(".")[0] + ".UTF-8"
    else:
        try: # macOS doesn't set LANG for apps.
            value = subprocess.run(["defaults", "read", "-g", "AppleLocale"], capture_output=True, text=True, timeout=5).stdout.strip()
            if re.match(r"^[a-z]{2,3}_[A-Z]{2}", value):
                locale = value.split("@")[0] + ".UTF-8"
        except (OSError, subprocess.SubprocessError):
            pass
    # English is generated too, many programs and logs expect it.
    locales = [locale or "en_US.UTF-8"] + ([] if locale in ("", "en_US.UTF-8") else ["en_US.UTF-8"])
    return LocalizationSettings(timezone=timezone, locales=locales, keymap=_local_keymap())

# Console keymaps for keyboard layouts of macOS (com.apple.keylayout.<name>), by name prefix.
_MACOS_KEYMAPS = {
    "Polish": "pl", "US": "us", "ABC": "us", "British": "uk", "German": "de", "Swiss German": "sg", "French": "fr",
    "Swiss French": "fr_CH", "Spanish": "es", "Italian": "it", "Portuguese": "pt-latin1", "Brazilian": "br-abnt2",
    "Czech": "cz", "Slovak": "sk", "Hungarian": "hu", "Russian": "ru", "Ukrainian": "ua", "Swedish": "se-lat6",
    "Norwegian": "no", "Danish": "dk", "Finnish": "fi", "Dutch": "nl", "Belgian": "be-latin1", "Turkish": "trq",
    "Dvorak": "dvorak",
}

def _local_keymap() -> str:
    try:
        value = subprocess.run(["defaults", "read", "com.apple.HIToolbox", "AppleCurrentKeyboardLayoutInputSourceID"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "us"
    name = value.removeprefix("com.apple.keylayout.")
    return next((keymap for prefix, keymap in sorted(_MACOS_KEYMAPS.items(), key=lambda item: -len(item[0])) if name.startswith(prefix.replace(" ", ""))), "us")

# ------------------------------------------------------------------------------
# SSH keys of this computer, authorized in deployed system:

@dataclass
class PublicKey:
    path: str
    key: str # Line for authorized_keys.

    @property
    def title(self) -> str:
        parts = self.key.split()
        return parts[2] if len(parts) > 2 else os.path.basename(self.path)

    @property
    def subtitle(self) -> str:
        return f"{self.key.split()[0]}, {self.path.replace(os.path.expanduser('~'), '~')}"

def local_public_keys() -> list[PublicKey]:
    keys = []
    for path in sorted(glob.glob(os.path.expanduser("~/.ssh/*.pub"))):
        try:
            with open(path, encoding="utf-8") as file:
                line = file.readline().strip()
            if line.startswith(("ssh-", "ecdsa-", "sk-")):
                keys.append(PublicKey(path=path, key=line))
        except OSError:
            pass
    return keys

def authorized_keys_script(keys: list[str], users: list[str]) -> str:
    """Adds keys to authorized_keys of root and given users."""
    if not keys:
        return ""
    content = quote("\n".join(keys))
    lines = []
    for user in ["root"] + users:
        home = '"$ROOT/root"' if user == "root" else f'"$ROOT/home/{user}"'
        lines += [
            f'mkdir -p {home}/.ssh && printf "%s\\n" {content} >> {home}/.ssh/authorized_keys',
            f'chmod 700 {home}/.ssh && chmod 600 {home}/.ssh/authorized_keys',
            f'chroot "$ROOT" chown -R {user}: {"/root" if user == "root" else "/home/" + user}/.ssh',
        ]
    lines.append(f'echo "Authorized {len(keys)} SSH key(s) for {", ".join(["root"] + users)}"')
    return "\n".join(lines)

@dataclass
class WifiSettings:
    """Wi-Fi network connected automatically (NetworkManager), eg. in live system prepared for given place."""
    ssid: str
    password: str = "" # WPA personal, empty for open network.
    hidden: bool = False
    country: str = "" # Regulatory domain (eg. PL), channels allowed in country.

    def error(self) -> str | None:
        if not self.ssid or len(self.ssid.encode()) > 32:
            return "Wi-Fi network name must have 1 to 32 bytes"
        if self.password and not 8 <= len(self.password) <= 63:
            return "Wi-Fi password must have 8 to 63 characters"
        if self.country and not re.match(r"^[A-Za-z]{2}$", self.country):
            return "Country must be two letter code, eg. PL"
        return None

WIFI_INTERFACE = "wlan0" # Wireless interface configured for services other than NetworkManager (name on Raspberry Pi).
WPA_SUPPLICANT_PACKAGE = "net-wireless/wpa_supplicant"
WPA_SUPPLICANT_FILE = re.compile(r"^usr/s?bin/wpa_supplicant$")

def _wpa_supplicant_config(settings: WifiSettings) -> str:
    """Configuration of wpa_supplicant: network name in hex, key derived from password (no quoting of special
    characters, password isn't stored as text)."""
    import hashlib
    lines = ["ctrl_interface=/run/wpa_supplicant", "ctrl_interface_group=wheel", "update_config=1"]
    if settings.country:
        lines.append(f"country={settings.country.upper()}")
    network = [f"    ssid={settings.ssid.encode().hex()}"]
    if settings.password:
        key = hashlib.pbkdf2_hmac("sha1", settings.password.encode(), settings.ssid.encode(), 4096, 32).hex()
        network.append(f"    psk={key}")
    else:
        network.append("    key_mgmt=NONE")
    if settings.hidden:
        network.append("    scan_ssid=1")
    return "\n".join(lines + ["network={", *network, "}"]) + "\n"

def wifi_script(settings: WifiSettings, service: NetworkService = NetworkService.NETWORK_MANAGER) -> str:
    """Wi-Fi network connected automatically by network service of $ROOT, and regulatory domain. NetworkManager gets
    its connection (readable by root only, it contains password), other services use wpa_supplicant on wlan0."""
    import base64
    script = []
    if service == NetworkService.NETWORK_MANAGER:
        lines = ["[connection]", f"id={settings.ssid}", "type=wifi", "autoconnect=true", "",
                 "[wifi]", "mode=infrastructure", f"ssid={settings.ssid}"] + (["hidden=true"] if settings.hidden else []) + [""]
        if settings.password:
            lines += ["[wifi-security]", "key-mgmt=wpa-psk", f"psk={settings.password}", ""]
        lines += ["[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
        encoded = base64.b64encode("\n".join(lines).encode()).decode()
        script += [
            '[ -d "$ROOT/etc/NetworkManager" ] || echo "Warning: NetworkManager is not installed, Wi-Fi connection is not used"',
            'mkdir -p "$ROOT/etc/NetworkManager/system-connections"',
            f'echo {encoded} | base64 -d > "$ROOT/etc/NetworkManager/system-connections/catalystlab-wifi.nmconnection"',
            'chmod 600 "$ROOT/etc/NetworkManager/system-connections/catalystlab-wifi.nmconnection"',
        ]
    elif service != NetworkService.NONE:
        interface = WIFI_INTERFACE
        encoded = base64.b64encode(_wpa_supplicant_config(settings).encode()).decode()
        config = f"$ROOT/etc/wpa_supplicant/wpa_supplicant-{interface}.conf" if service == NetworkService.SYSTEMD_NETWORKD \
            else "$ROOT/etc/wpa_supplicant/wpa_supplicant.conf"
        script += [
            'mkdir -p "$ROOT/etc/wpa_supplicant"',
            f'echo {encoded} | base64 -d > "{config}"',
            f'chmod 600 "{config}"',
        ]
        if service == NetworkService.SYSTEMD_NETWORKD:
            network = ["[Match]", "Name=wl*", "", "[Network]", "DHCP=yes"]
            script += [enable_systemd_instance_script("wpa_supplicant", interface),
                       'mkdir -p "$ROOT/etc/systemd/network"',
                       f'printf "%s\\n" {quote(chr(10).join(network))} > "$ROOT/etc/systemd/network/25-wireless.network"']
        elif service == NetworkService.DHCPCD:
            # wpa_supplicant connects network, dhcpcd gets address of interface.
            script += [
                init_system_script(),
                'if [ "$CL_INIT_SYSTEM" = openrc ]; then',
                f'    [ -f "$ROOT/etc/conf.d/wpa_supplicant" ] && sed -i \'s|^wpa_supplicant_args=.*|wpa_supplicant_args="-i {interface}"|\' "$ROOT/etc/conf.d/wpa_supplicant"',
                f'    grep -q "^wpa_supplicant_args=" "$ROOT/etc/conf.d/wpa_supplicant" 2>/dev/null || echo \'wpa_supplicant_args="-i {interface}"\' >> "$ROOT/etc/conf.d/wpa_supplicant"',
                '    ' + enable_service_script("wpa_supplicant", None),
                'else',
                f'    ln -sf wpa_supplicant.conf "$ROOT/etc/wpa_supplicant/wpa_supplicant-{interface}.conf"',
                '    ' + enable_systemd_instance_script("wpa_supplicant", interface),
                'fi',
            ]
        elif service == NetworkService.NETIFRC:
            config_lines = [f'modules_{interface}="wpa_supplicant"', f'config_{interface}="dhcp"']
            script += [
                f'printf "%s\\n" {quote(chr(10).join(config_lines))} >> "$ROOT/etc/conf.d/net"',
                f'ln -sf net.lo "$ROOT/etc/init.d/net.{interface}"',
                f'ln -sf /etc/init.d/net.{interface} "$ROOT/etc/runlevels/default/net.{interface}" && echo "Enabled net.{interface} (OpenRC)"',
            ]
    script.append(f'echo "Wi-Fi network connected automatically:" {quote(settings.ssid)}')
    if settings.country:
        country = settings.country.upper()
        script += [f'mkdir -p "$ROOT/etc/modprobe.d" && echo "options cfg80211 ieee80211_regdom={country}" > "$ROOT/etc/modprobe.d/catalystlab-wifi.conf"',
                   f'echo "Wi-Fi country: {country}"']
    return "\n".join(script)
