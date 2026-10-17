from pathlib import Path
import os
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import project_stage_cache
from catalystlab.project_stage_arguments import StageArgumentDetails
from catalystlab.project_stage_automatic_option import StageAutomaticOption


class StageCachePathDisplayTests(unittest.TestCase):
    def test_home_paths_use_tilde_and_external_paths_stay_absolute(self):
        home = os.path.expanduser('~')
        self.assertEqual(project_stage_cache.display_path(os.path.join(home, 'CatalystLab')), '~/CatalystLab')
        self.assertEqual(project_stage_cache.display_path('/tmp/cache'), '/tmp/cache')


class StageCachePathTests(unittest.TestCase):
    def test_manual_paths_expand_home_and_empty_paths_disable_cache(self):
        stage = types.SimpleNamespace(pkgcache_path='~/packages')
        self.assertEqual(project_stage_cache.stage_cache_path(None, stage, StageArgumentDetails.pkgcache_path),
                         os.path.abspath(os.path.expanduser('~/packages')))
        stage.pkgcache_path = '  '
        self.assertIsNone(project_stage_cache.stage_cache_path(None, stage, StageArgumentDetails.pkgcache_path))

    def test_automatic_package_and_kernel_caches_use_their_intended_roots(self):
        packages = types.SimpleNamespace(directory_path=lambda: '/library/Packages/project')
        project = types.SimpleNamespace(get_packages_directory=lambda: packages)
        stage = types.SimpleNamespace(pkgcache_path=StageAutomaticOption.GENERATE_AUTOMATICALLY,
                                      kerncache_path=StageAutomaticOption.GENERATE_AUTOMATICALLY)
        with patch('catalystlab.project_build.project_builds_directory', return_value='/library/Builds/project'), \
             patch('catalystlab.project_stage_value_resolver.resolve_stage_argument', return_value='23.0'):
            package_cache = project_stage_cache.stage_cache_path(
                project, stage, StageArgumentDetails.pkgcache_path)
            kernel_cache = project_stage_cache.stage_cache_path(
                project, stage, StageArgumentDetails.kerncache_path)

        self.assertEqual(package_cache, '/library/Packages/project/23.0')
        self.assertEqual(kernel_cache, '/library/Builds/project/caches/kerncache/23.0')

    def test_automatic_cache_rejects_rel_type_path_traversal(self):
        packages = types.SimpleNamespace(directory_path=lambda: '/library/Packages/project')
        project = types.SimpleNamespace(get_packages_directory=lambda: packages)
        stage = types.SimpleNamespace(pkgcache_path=StageAutomaticOption.GENERATE_AUTOMATICALLY)
        with patch('catalystlab.project_stage_value_resolver.resolve_stage_argument',
                   return_value='../../outside'):
            result = project_stage_cache.stage_cache_path(project, stage, StageArgumentDetails.pkgcache_path)
        self.assertIsNone(result)

    def test_automatic_cache_rejects_paths_on_incompatible_roots(self):
        packages = types.SimpleNamespace(directory_path=lambda: '/library/Packages/project')
        project = types.SimpleNamespace(get_packages_directory=lambda: packages)
        stage = types.SimpleNamespace(pkgcache_path=StageAutomaticOption.GENERATE_AUTOMATICALLY)
        with patch('catalystlab.project_stage_value_resolver.resolve_stage_argument', return_value='23.0'), \
             patch('catalystlab.project_stage_cache.os.path.commonpath', side_effect=ValueError):
            result = project_stage_cache.stage_cache_path(project, stage, StageArgumentDetails.pkgcache_path)

        self.assertIsNone(result)

    def test_automatic_cache_waits_until_rel_type_is_available(self):
        stage = types.SimpleNamespace(pkgcache_path=StageAutomaticOption.GENERATE_AUTOMATICALLY)
        with patch('catalystlab.project_stage_value_resolver.resolve_stage_argument', return_value=None):
            result = project_stage_cache.stage_cache_path(None, stage, StageArgumentDetails.pkgcache_path)
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()
