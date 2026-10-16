"""Validation and configuration generation for deployed networking and accounts."""
import base64
import os
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.deploy_system import (
    LocalizationSettings, NetworkService, NetworkSettings, PublicKey, WifiSettings,
    _local_keymap, _wpa_supplicant_config, authorized_keys_script,
    default_network_service, local_localization, local_public_keys,
    localization_script, network_script, wifi_script,
)


class NetworkSettingsTests(unittest.TestCase):
    def test_default_service_prefers_supported_existing_service(self):
        available = {NetworkService.NETIFRC, NetworkService.SYSTEMD_NETWORKD, NetworkService.DHCPCD}
        self.assertEqual(default_network_service(available, 'systemd'), NetworkService.SYSTEMD_NETWORKD)
        self.assertEqual(default_network_service(available, 'openrc'), NetworkService.DHCPCD)
        self.assertEqual(default_network_service(set(), None), NetworkService.DHCPCD)
        self.assertFalse(NetworkService.NETWORK_MANAGER.supported(None) is False)
        self.assertFalse(NetworkService.SYSTEMD_NETWORKD.supported('openrc'))
        self.assertTrue(NetworkService.NETIFRC.supported('openrc'))

    def test_static_settings_validate_interface_ip_prefix_gateway_and_dns(self):
        valid = NetworkSettings(NetworkService.DHCPCD, True, 'enp1s0', '192.0.2.4/24', '192.0.2.1', '192.0.2.53, 2001:db8::53')
        self.assertIsNone(valid.error())
        self.assertEqual(valid.dns_servers, ['192.0.2.53', '2001:db8::53'])
        self.assertIsNone(NetworkSettings().error())
        for values in (
            dict(interface=''), dict(interface='eth 0'), dict(interface='eth0', address='192.0.2.4'),
            dict(interface='eth0', address='invalid/24'), dict(interface='eth0', address='192.0.2.4/24', gateway='bad'),
            dict(interface='eth0', address='192.0.2.4/24', dns='192.0.2.53,bad'),
        ):
            settings = NetworkSettings(NetworkService.DHCPCD, True, **values)
            self.assertTrue(settings.error(), values)

    def test_network_manager_static_profile_contains_escaped_settings(self):
        settings = NetworkSettings(NetworkService.NETWORK_MANAGER, True, 'enp1s0', '192.0.2.4/24', '192.0.2.1', '192.0.2.53')
        script = network_script(settings)
        self.assertIn('NetworkManager', script)
        self.assertIn('chmod 600', script)
        self.assertIn('wired.nmconnection', script)
        self.assertIn('dns=192.0.2.53;', script)
        self.assertIn('nameserver %s\\n" 192.0.2.53', script)
        self.assertIn('method=manual', script)

    def test_networkd_dhcp_and_static_profiles(self):
        dhcp = network_script(NetworkSettings(NetworkService.SYSTEMD_NETWORKD))
        self.assertIn('DHCP=yes', dhcp)
        self.assertIn('Name=en* eth*', dhcp)
        static = network_script(NetworkSettings(NetworkService.SYSTEMD_NETWORKD, True, 'enp2s0',
                                                '2001:db8::10/64', '2001:db8::1', '2001:db8::53'))
        self.assertIn('Address=2001:db8::10/64', static)
        self.assertIn('Gateway=2001:db8::1', static)
        self.assertIn('DNS=2001:db8::53', static)
        self.assertIn('systemd-resolved', static)

    def test_dhcpcd_and_netifrc_static_and_dhcp_profiles(self):
        dhcp = network_script(NetworkSettings(NetworkService.DHCPCD))
        self.assertIn('dhcpcd', dhcp)
        self.assertNotIn('static ip_address', dhcp)
        static = network_script(NetworkSettings(NetworkService.DHCPCD, True, 'eth0', '192.0.2.4/24', '192.0.2.1', '1.1.1.1'))
        self.assertIn('static routers=192.0.2.1', static)
        self.assertIn('static domain_name_servers=1.1.1.1', static)
        netifrc = network_script(NetworkSettings(NetworkService.NETIFRC, True, 'enp4s0', '192.0.2.4/24', '192.0.2.1', '1.1.1.1'))
        self.assertIn('config_enp4s0="192.0.2.4/24"', netifrc)
        self.assertIn('routes_enp4s0="default via 192.0.2.1"', netifrc)
        self.assertIn('dns_servers_enp4s0="1.1.1.1"', netifrc)
        self.assertIn('net.enp4s0', netifrc)
        self.assertEqual(network_script(NetworkSettings()), '')


class WifiSettingsTests(unittest.TestCase):
    def test_wifi_validation_uses_utf8_bytes_and_password_limits(self):
        self.assertEqual(WifiSettings('').error(), 'Wi-Fi network name must have 1 to 32 bytes')
        self.assertEqual(WifiSettings('é' * 17).error(), 'Wi-Fi network name must have 1 to 32 bytes')
        self.assertIsNone(WifiSettings('é' * 16).error())
        self.assertTrue(WifiSettings('wifi', 'short').error())
        self.assertTrue(WifiSettings('wifi', 'p' * 64).error())
        self.assertTrue(WifiSettings('wifi', country='POL').error())
        self.assertIsNone(WifiSettings('wifi', country='pl').error())

    def test_wpa_configuration_encodes_ssid_and_derives_psk(self):
        config = _wpa_supplicant_config(WifiSettings('My Wi-Fi', 'correct horse battery', hidden=True, country='pl'))
        self.assertIn('country=PL', config)
        self.assertIn('ssid=4d792057692d4669', config)
        self.assertIn('scan_ssid=1', config)
        self.assertNotIn('correct horse battery', config)
        self.assertRegex(config, r'psk=[0-9a-f]{64}')
        open_network = _wpa_supplicant_config(WifiSettings('Guest'))
        self.assertIn('key_mgmt=NONE', open_network)
        self.assertNotIn('psk=', open_network)

    def test_wifi_scripts_cover_networkmanager_networkd_dhcpcd_netifrc_and_none(self):
        settings = WifiSettings('Office Wi-Fi', 'password123', hidden=True, country='us')
        manager = wifi_script(settings)
        command = next(line for line in manager.splitlines() if 'base64 -d' in line)
        encoded = re.search(r'echo ([A-Za-z0-9+/=]+) \| base64 -d', command).group(1)
        profile = base64.b64decode(encoded).decode()
        self.assertIn('id=Office Wi-Fi', profile)
        self.assertIn('hidden=true', profile)
        self.assertIn('psk=password123', profile)
        self.assertIn('chmod 600', manager)
        networkd = wifi_script(settings, NetworkService.SYSTEMD_NETWORKD)
        self.assertIn('wpa_supplicant-wlan0.conf', networkd)
        self.assertIn('wpa_supplicant@wlan0.service', networkd)
        dhcpcd = wifi_script(settings, NetworkService.DHCPCD)
        self.assertIn('wpa_supplicant_args="-i wlan0"', dhcpcd)
        netifrc = wifi_script(settings, NetworkService.NETIFRC)
        self.assertIn('modules_wlan0="wpa_supplicant"', netifrc)
        none = wifi_script(settings, NetworkService.NONE)
        self.assertNotIn('wpa_supplicant', none)
        for script in (manager, networkd, dhcpcd, netifrc, none):
            self.assertIn('ieee80211_regdom=US', script)


class LocalizationAndKeysTests(unittest.TestCase):
    def test_localization_validation_and_script_generation(self):
        valid = LocalizationSettings('Europe/Warsaw', ['pl_PL.UTF-8', 'en_US.UTF-8'], 'pl')
        self.assertIsNone(valid.error())
        self.assertTrue(LocalizationSettings('../etc/passwd').error())
        self.assertTrue(LocalizationSettings(locales=['bad locale']).error())
        self.assertTrue(LocalizationSettings(locales=['en_US.UTF-8', 'en_US.UTF-8']).error())
        self.assertTrue(LocalizationSettings(keymap='bad keymap').error())
        script = localization_script(valid)
        self.assertIn('Europe/Warsaw', script)
        self.assertIn('pl_PL.UTF-8 UTF-8', script)
        self.assertIn('LANG=pl_PL.UTF-8', script)
        self.assertIn('LANG=\\"pl_PL.UTF-8\\"', script)
        self.assertIn('KEYMAP=pl', script)
        minimal = localization_script(LocalizationSettings(locales=['C', 'en_US.UTF-8']))
        self.assertNotIn('C UTF-8', minimal)
        self.assertIn('locale-gen', minimal)
        self.assertEqual(localization_script(LocalizationSettings()), '')

    def test_host_localization_uses_locale_fallbacks_and_keymap_mapping(self):
        with patch.dict(os.environ, {'LANG': 'pl_PL.UTF-8'}), \
             patch('catalystlab.deploy_system.os.path.realpath', return_value='/usr/share/zoneinfo/Europe/Warsaw'), \
             patch('catalystlab.deploy_system._local_keymap', return_value='pl'):
            value = local_localization()
        self.assertEqual(value, LocalizationSettings('Europe/Warsaw', ['pl_PL.UTF-8', 'en_US.UTF-8'], 'pl'))
        with patch('catalystlab.deploy_system.subprocess.run', return_value=types.SimpleNamespace(stdout='com.apple.keylayout.Polish')):
            self.assertEqual(_local_keymap(), 'pl')
        with patch('catalystlab.deploy_system.subprocess.run', side_effect=FileNotFoundError()):
            self.assertEqual(_local_keymap(), 'us')

    def test_public_key_labels_and_authorized_keys_script(self):
        key = PublicKey(str(Path.home() / '.ssh/id_ed25519.pub'), 'ssh-ed25519 AAAA user@host')
        self.assertEqual(key.title, 'user@host')
        self.assertEqual(key.subtitle, 'ssh-ed25519, ~/.ssh/id_ed25519.pub')
        fallback = PublicKey('/tmp/key.pub', 'ssh-rsa AAAA')
        self.assertEqual(fallback.title, 'key.pub')
        self.assertEqual(authorized_keys_script([], ['alice']), '')
        script = authorized_keys_script([key.key], ['alice'])
        self.assertIn('"$ROOT/root"/.ssh', script)
        self.assertIn('"$ROOT/home/alice"/.ssh', script)
        self.assertIn('chmod 700', script)
        self.assertIn('alice', script)

    def test_public_key_scan_accepts_supported_types_and_ignores_bad_files(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            ssh = home / '.ssh'
            ssh.mkdir()
            (ssh / 'a.pub').write_text('ssh-ed25519 AAAA first@host\ncomment\n')
            (ssh / 'b.pub').write_text('ecdsa-sha2-nistp256 BBBB second@host\n')
            (ssh / 'c.pub').write_text('sk-ssh-ed25519@openssh.com CCCC third@host\n')
            (ssh / 'bad.pub').write_text('not a public key\n')
            with patch('catalystlab.deploy_system.os.path.expanduser', side_effect=lambda path: str(home) if path == '~' else str(home / path.removeprefix('~/'))), \
                 patch('catalystlab.deploy_system.glob.glob', return_value=[str(path) for path in sorted(ssh.glob('*.pub'))]):
                keys = local_public_keys()
        self.assertEqual([key.title for key in keys], ['first@host', 'second@host', 'third@host'])


if __name__ == '__main__':
    unittest.main()
