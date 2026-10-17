"""Event subscriptions use weak references and replace reusable handles."""
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.event_bus import EventBus, SharedEvent
from catalystlab.app_events import AppEvents


class EventBusTests(unittest.TestCase):
    def test_application_events_have_unique_enum_values(self):
        values = [event.value for event in AppEvents]
        self.assertTrue(all(type(value) is int for value in values))
        self.assertEqual(len(values), len(set(values)))

    def test_reusing_handle_replaces_previous_callback(self):
        received = []
        bus = EventBus(scheduler=lambda callback, *args: callback(*args))

        def first(value):
            received.append(('first', value))

        def second(value):
            received.append(('second', value))

        bus.subscribe(SharedEvent.STATE_UPDATED, first, handle='view')
        bus.subscribe(SharedEvent.STATE_UPDATED, second, handle='view')
        bus.emit(SharedEvent.STATE_UPDATED, 1)

        self.assertEqual(received, [('second', 1)])
        bus.unsubscribe(SharedEvent.STATE_UPDATED, 'view')
        bus.emit(SharedEvent.STATE_UPDATED, 2)
        self.assertEqual(received, [('second', 1)])

    def test_subscriptions_added_during_emit_start_on_next_emit(self):
        received = []
        bus = EventBus(scheduler=lambda callback, *args: callback(*args))

        def added(value):
            received.append(('added', value))

        def add_subscriber(value):
            received.append(('original', value))
            if value == 1:
                bus.subscribe(SharedEvent.STATE_UPDATED, added)

        bus.subscribe(SharedEvent.STATE_UPDATED, add_subscriber)
        bus.emit(SharedEvent.STATE_UPDATED, 1)
        self.assertEqual(received, [('original', 1)])

        bus.emit(SharedEvent.STATE_UPDATED, 2)
        self.assertEqual(received, [('original', 1), ('original', 2), ('added', 2)])


if __name__ == '__main__':
    unittest.main()
