"""Application section registration contracts."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

Adw = types.SimpleNamespace(NavigationView=type('NavigationView', (), {}))
fake_gi = types.ModuleType('gi')
fake_repository = types.ModuleType('gi.repository')
fake_repository.Adw = Adw
fake_gi.repository = fake_repository
with patch.dict(sys.modules, {'gi': fake_gi, 'gi.repository': fake_repository}):
    from catalystlab.app_section import AppSection, app_section


class AppSectionTests(unittest.TestCase):
    def test_decorator_registers_section_and_sorts_by_order(self):
        previous = AppSection.all_sections[:]
        try:
            @app_section(title='Temporary test', order=-1)
            class TemporaryTestSection:
                def __init__(self, content_navigation_view: Adw.NavigationView, **kwargs):
                    pass

            self.assertIs(AppSection.all_sections[0], TemporaryTestSection)
            self.assertIs(AppSection.TemporaryTestSection, TemporaryTestSection)
            self.assertIs(TemporaryTestSection.section_details.cls, TemporaryTestSection)
        finally:
            AppSection.all_sections[:] = previous
            delattr(AppSection, 'TemporaryTestSection')


if __name__ == '__main__':
    unittest.main()
