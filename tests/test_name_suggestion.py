import unittest

from catalystlab.name_suggestion import unique_name


class UniqueNameTests(unittest.TestCase):
    def test_keeps_available_trimmed_name(self):
        self.assertEqual(unique_name("  Build machine  ", lambda name: True), "Build machine")

    def test_adds_next_available_suffix(self):
        unavailable = {"Build machine", "Build machine - 2", "Build machine - 3"}
        self.assertEqual(unique_name("Build machine", lambda name: name not in unavailable), "Build machine - 4")

    def test_keeps_empty_name_empty(self):
        self.assertEqual(unique_name("  ", lambda name: False), "")


if __name__ == "__main__":
    unittest.main()
