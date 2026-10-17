"""Loading Gentoo stage3 URLs for toolsets and build seeds."""
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

from catalystlab import toolset_env_builder
from catalystlab.architecture import Architecture


class ToolsetEnvironmentBuilderTests(unittest.TestCase):
    def test_fetches_only_stage3_archives_for_selected_architecture(self):
        response = Mock(text='20261001/stage3-amd64-openrc-20261001T000000Z.tar.xz\nREADME\nother/file.tar.gz\n')
        with patch.object(toolset_env_builder.requests, 'get', return_value=response) as get:
            result = toolset_env_builder.get_stage3_urls(Architecture.amd64)

        response.raise_for_status.assert_called_once_with()
        get.assert_called_once_with('https://distfiles.gentoo.org/releases/amd64/autobuilds/latest-stage3.txt')
        self.assertEqual([url.path for url in result], [
            '/releases/amd64/autobuilds/20261001/stage3-amd64-openrc-20261001T000000Z.tar.xz',
        ])

    def test_returns_request_failure_for_the_caller_to_report(self):
        error = toolset_env_builder.requests.RequestException('offline')
        with patch.object(toolset_env_builder.requests, 'get', side_effect=error), patch('builtins.print'):
            self.assertIs(toolset_env_builder.get_stage3_urls(Architecture.arm64), error)


if __name__ == '__main__':
    unittest.main()
