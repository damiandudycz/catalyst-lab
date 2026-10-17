"""Persistence behavior for application preferences and tracked collections."""
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import repository


class SerializableItem:
    def __init__(self, value):
        self.value = value

    def serialize(self):
        return {'value': self.value}

    @classmethod
    def init_from(cls, data):
        return cls(data['value'])

    @classmethod
    def load_all(cls):
        return [cls('loaded')]

    @classmethod
    def save_all(cls, values):
        cls.saved = list(values)


class RepositoryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.aliases = repository.Repository.registered_aliases[:]
        self.addCleanup(lambda: repository.Repository.registered_aliases.__setitem__(slice(None), self.aliases))

    def test_preferences_repository_loads_saved_value_or_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            preferences = Path(temporary, 'preferences.json')
            preferences.write_text('{"value": "saved"}')
            with patch.object(repository, 'preferences_file', return_value=str(preferences)):
                saved = repository.Repository(SerializableItem, alias='unit.saved', default_factory=lambda: SerializableItem('default'))
            self.assertEqual(saved.value.value, 'saved')
            preferences.unlink()
            with patch.object(repository, 'preferences_file', return_value=str(preferences)):
                default = repository.Repository(SerializableItem, alias='unit.default', default_factory=lambda: SerializableItem('default'))
            self.assertEqual(default.value.value, 'default')

    def test_invalid_preferences_use_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, 'preferences.json')
            path.write_text('{invalid')
            with patch.object(repository, 'preferences_file', return_value=str(path)):
                repo = repository.Repository(SerializableItem, alias='unit.invalid', default_factory=lambda: SerializableItem('fallback'))
        self.assertEqual(repo.value.value, 'fallback')

    def test_preferences_save_serializes_value_and_emits_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, 'nested', 'preferences.json')
            with patch.object(repository, 'preferences_file', return_value=str(path)):
                repo = repository.Repository(SerializableItem, alias='unit.save', default_factory=lambda: SerializableItem('initial'))
                events = []
                repo.event_bus.scheduler = lambda callback, *args: callback(*args)
                def changed(value):
                    events.append(value)
                repo.event_bus.subscribe(repository.RepositoryEvent.VALUE_CHANGED, changed)
                repo.value = SerializableItem('updated')
            self.assertEqual(json.loads(path.read_text()), {'value': 'updated'})
            self.assertEqual(events, [repo.value])

    def test_collection_loads_and_mutations_persist(self):
        with patch.object(repository, 'preferences_file') as preferences:
            preferences.side_effect = AssertionError('collection repositories use load_all/save_all')
            repo = repository.Repository(SerializableItem, collection=True, alias='unit.collection')
        self.assertEqual([item.value for item in repo.value], ['loaded'])
        repo.value.append(SerializableItem('next'))
        self.assertEqual([item.value for item in SerializableItem.saved], ['loaded', 'next'])
        repo.value = [SerializableItem('replacement')]
        self.assertEqual([item.value for item in SerializableItem.saved], ['replacement'])

    def test_tracked_list_mutators_each_trigger_save(self):
        calls = []
        values = repository.TrackedList([3, 1, 2], lambda: calls.append('save'))
        values.append(4)
        values.extend([5])
        values.insert(0, 0)
        values.remove(5)
        values.pop()
        values[0] = 8
        del values[0]
        values.sort()
        values.reverse()
        values += [6]
        values *= 2
        values.clear()
        self.assertEqual(len(calls), 12)

    def test_augmented_collection_assignment_saves_once(self):
        repo = repository.Repository(SerializableItem, collection=True, alias='unit.augmented')
        repo.save = Mock()
        repo.value._save_callback = repo.save

        repo.value += [SerializableItem('added')]
        repo.value *= 2

        self.assertEqual(repo.save.call_count, 2)

    def test_collection_rejects_non_list_assignment(self):
        repo = repository.Repository(SerializableItem, collection=True, alias='unit.typecheck')
        with self.assertRaisesRegex(TypeError, 'Expected a list or TrackedList'):
            repo.value = 'not a list'


class LibraryLocationTests(unittest.TestCase):
    def test_preferences_and_library_settings_share_config_root(self):
        with patch.object(repository, '_system_config_directory', return_value='/config'):
            self.assertEqual(repository.preferences_file(), '/config/catalystlab/preferences.json')
            self.assertEqual(repository._library_location_file(), '/config/catalystlab-library.json')

    def test_library_location_is_loaded_and_cached(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary, 'config')
            config.mkdir()
            location_file = config/'catalystlab-library.json'
            library = Path(temporary, 'Library')
            location_file.write_text(json.dumps({'library': str(library)}))
            with patch.object(repository, '_library_directory', None), \
                 patch.object(repository, '_system_config_directory', return_value=str(config)):
                self.assertEqual(repository.library_directory(), str(library))
                location_file.write_text(json.dumps({'library': str(Path(temporary, 'changed'))}))
                self.assertEqual(repository.library_directory(), str(library))
            repository._library_directory = None

    def test_store_library_directory_writes_atomically_and_updates_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary, 'config')
            library = Path(temporary, 'Library')
            with patch.object(repository, '_system_config_directory', return_value=str(config)), \
                 patch.object(repository, '_library_directory', None):
                repository.store_library_directory(str(library))
                self.assertEqual(repository.library_directory(), str(library))
                location_file = config/'catalystlab-library.json'
                self.assertEqual(json.loads(location_file.read_text()), {'library': str(library)})
                self.assertFalse(Path(str(location_file)+'.tmp').exists())
            repository._library_directory = None

    def test_collapse_home_shrinks_home_paths_only_at_directory_boundary(self):
        with patch('catalystlab.repository.os.path.expanduser', side_effect=lambda path: '/home/test' if path == '~' else path):
            self.assertEqual(repository.collapse_home('/home/test/Library'), '~/Library')
            self.assertEqual(repository.collapse_home('/home/tester/Library'), '/home/tester/Library')


if __name__ == '__main__':
    unittest.main()
