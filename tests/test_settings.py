"""Settings defaults, serialization, change events, and persistence."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import settings as settings_module
from catalystlab.settings import NavigationStyle, Settings, SettingsEvents


class SettingsTests(unittest.TestCase):
    def test_defaults_and_serialization_round_trip(self):
        settings = Settings()
        self.assertFalse(settings.initial_setup_done)
        self.assertTrue(settings.keep_root_unlocked)
        self.assertFalse(settings.auto_clean_snapshots)
        self.assertEqual(settings.navigation_style, NavigationStyle.PUSH)
        self.assertFalse(settings.pause_queue_on_failure)
        self.assertEqual(settings.qemu_memory_gib, 4)
        self.assertEqual(settings.qemu_cpus, 4)
        self.assertEqual(Settings.init_from(settings.serialize()).serialize(), settings.serialize())

    def test_missing_or_invalid_navigation_style_falls_back_to_push(self):
        self.assertEqual(Settings.init_from({}).navigation_style, NavigationStyle.PUSH)
        self.assertEqual(Settings.init_from({'navigation_style': 'unknown'}).navigation_style, NavigationStyle.PUSH)
        self.assertEqual(Settings.init_from({'navigation_style': None}).navigation_style, NavigationStyle.PUSH)
        self.assertEqual(Settings.init_from({'navigation_style': 'modal'}).navigation_style, NavigationStyle.MODAL)

    def test_boolean_preference_changes_emit_events_and_persist_once(self):
        settings = Settings()
        repository = types.SimpleNamespace(save=Mock())
        with patch.object(settings_module.Repository, 'Settings', repository, create=True):
            settings.event_bus.scheduler = lambda callback, *args: callback(*args)
            events = []
            def collect(value):
                events.append(value)
            settings.event_bus.subscribe(SettingsEvents.AUTO_CLEAN_SNAPSHOTS_CHANGED, collect)
            settings.auto_clean_snapshots = True
            settings.auto_clean_snapshots = True
        repository.save.assert_called_once_with()
        self.assertEqual(events, [True])

    def test_each_boolean_setting_uses_its_own_change_event(self):
        settings = Settings()
        repository = types.SimpleNamespace(save=Mock())
        with patch.object(settings_module.Repository, 'Settings', repository, create=True):
            settings.event_bus.scheduler = lambda callback, *args: callback(*args)
            events = []
            callbacks = []
            for event in SettingsEvents:
                def collect(value, event=event):
                    events.append((event, value))
                callbacks.append(collect)
                settings.event_bus.subscribe(event, collect)
            settings.initial_setup_done = True
            settings.keep_root_unlocked = False
            settings.auto_clean_snapshots = True
            settings.navigation_style = NavigationStyle.MODAL
            settings.pause_queue_on_failure = True
        self.assertEqual(repository.save.call_count, 5)
        self.assertEqual({event for event, _ in events}, set(SettingsEvents))

    def test_qemu_resource_settings_coerce_integers_and_skip_unchanged_saves(self):
        settings = Settings()
        repository = types.SimpleNamespace(save=Mock())
        with patch.object(settings_module.Repository, 'Settings', repository, create=True):
            settings.qemu_memory_gib = '12'
            settings.qemu_memory_gib = 12
            settings.qemu_cpus = '8'
            settings.qemu_cpus = 8
        self.assertEqual(settings.qemu_memory_gib, 12)
        self.assertEqual(settings.qemu_cpus, 8)
        self.assertEqual(repository.save.call_count, 2)

    def test_library_data_paths_follow_selected_library(self):
        expected = {
            'toolsets_location': 'Toolsets', 'snapshots_location': 'Snapshots', 'releng_location': 'Releng',
            'overlay_location': 'Overlays', 'project_location': 'Projects', 'builds_location': 'Builds',
            'cache_location': 'Cache', 'temporary_location': 'Temporary', 'packages_location': 'Packages',
        }
        for property_name, folder in expected.items():
            with self.subTest(property_name=property_name), patch.object(settings_module, 'library_path', return_value=f'/library/{folder}') as path:
                self.assertEqual(getattr(Settings(), property_name), f'/library/{folder}')
                path.assert_called_once_with(folder)


if __name__ == '__main__':
    unittest.main()
