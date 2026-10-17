"""Portage and filesystem overlay inheritance and cleanup rules."""
from pathlib import Path
import os
import sys
import tempfile
import types
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT / 'src')] + [str(path) for path in (ROOT / 'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.architecture import Architecture
from catalystlab.project_stage_automatic_option import StageAutomaticOption
from catalystlab.project_stage_arguments import StageArgumentDetails
from catalystlab.project_stage_portage_confdir import (
    StagePortageConfdirSource as Source, count_files, default_portage_confdir_sources,
    overlay_stages, parent_overlay_stages, portage_confdir_options, portage_confdir_sources,
    parent_root_overlay_stages, releng_portage_variant, remove_inherited_files, remove_stage_overlays_inherited_files,
    root_overlay_argument, root_overlay_folders, root_overlay_options, root_overlay_stages,
    root_overlay_values, stage_overlay_path, stage_root_overlay_path,
)


class FakeProject:
    def __init__(self, directory, stages=(), releng=None, architecture=Architecture.arm64):
        self.directory = str(directory)
        self.stages = list(stages)
        self.releng = releng
        self.architecture = architecture

    def stage_directory_path(self, name):
        return os.path.join(self.directory, 'stages', name)

    def directory_path(self):
        return self.directory

    def get_releng_directory(self):
        return self.releng

    def get_architecture(self):
        return self.architecture


def stage(name, target='stage4', parent=None, portage=None, root_overlay=None, livecd_overlay=None):
    return types.SimpleNamespace(id=uuid.uuid4(), name=name, target=target, parent=parent,
        portage_confdir=portage, stage4_root_overlay=root_overlay, livecd_root_overlay=root_overlay,
        livecd_overlay=livecd_overlay)


class PortageOverlayTests(unittest.TestCase):
    def test_source_defaults_deduplicate_and_remain_in_application_order(self):
        self.assertEqual(default_portage_confdir_sources(False), [Source.RELENG])
        self.assertEqual(default_portage_confdir_sources(True), [Source.RELENG, Source.PARENT_OVERLAY])
        child = stage('child', portage=[Source.STAGE_OVERLAY, Source.RELENG, Source.STAGE_OVERLAY])
        self.assertEqual(portage_confdir_sources(child), [Source.RELENG, Source.STAGE_OVERLAY])

    def test_portage_overlay_inheritance_stops_when_parent_disables_inheritance(self):
        root = stage('root', portage=[Source.STAGE_OVERLAY])
        middle = stage('middle', parent=root.id, portage=[Source.PARENT_OVERLAY, Source.STAGE_OVERLAY])
        child = stage('child', parent=middle.id, portage=[Source.PARENT_OVERLAY])
        project = FakeProject('/project', [root, middle, child])
        self.assertEqual(overlay_stages(project, child), [root, middle])
        self.assertEqual(parent_overlay_stages(project, child), [root, middle])
        middle.portage_confdir = [Source.STAGE_OVERLAY]
        self.assertEqual(overlay_stages(project, child), [middle])

    def test_portage_overlay_cycle_terminates(self):
        first = stage('first', parent=None, portage=[Source.PARENT_OVERLAY, Source.STAGE_OVERLAY])
        second = stage('second', parent=first.id, portage=[Source.PARENT_OVERLAY, Source.STAGE_OVERLAY])
        first.parent = second.id
        project = FakeProject('/project', [first, second])
        self.assertEqual(overlay_stages(project, first), [second, first])

    def test_variant_selection_prefers_existing_emulation_and_architecture_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            portage = Path(directory) / 'releases' / 'portage'
            (portage / 'stages-qemu').mkdir(parents=True)
            releng = types.SimpleNamespace(directory_path=lambda: directory)
            project = FakeProject('/project', releng=releng, architecture=Architecture.amd64)
            self.assertEqual(releng_portage_variant(project, stage('s', target='stage3')), 'stages-qemu')
            (portage / 'isos-x86').mkdir()
            project.architecture = Architecture.x86
            self.assertEqual(releng_portage_variant(project, stage('s', target='livecd_stage1')), 'isos-x86')
        project = FakeProject('/project', architecture=None)
        self.assertIsNone(releng_portage_variant(project, stage('s')))

    def test_overlay_options_report_file_counts_and_parent_availability(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = stage('parent', target='stage3', portage=[Source.STAGE_OVERLAY])
            child = stage('child', parent=parent.id, portage=[Source.PARENT_OVERLAY])
            project = FakeProject(directory, [parent, child])
            for item, filename in ((parent, 'parent.conf'), (child, 'child.conf')):
                folder = Path(stage_overlay_path(project, item))
                folder.mkdir(parents=True)
                (folder / filename).write_text('x')
            options = portage_confdir_options(project, child)
            self.assertIn('1 file from parent', options[1].subtitle)
            self.assertIn('1 file, overwrite', options[2].subtitle)
            self.assertFalse(options[1].unsupported)
            orphan = portage_confdir_options(project, parent)
            self.assertTrue(orphan[1].unsupported)


class RootOverlayTests(unittest.TestCase):
    def test_root_overlay_argument_and_sources(self):
        self.assertEqual(root_overlay_argument(stage('s', target='stage4')), StageArgumentDetails.stage4_root_overlay)
        self.assertEqual(root_overlay_argument(stage('s', target='livecd_stage2')), StageArgumentDetails.livecd_root_overlay)
        self.assertIsNone(root_overlay_argument(stage('s', target='stage3')))
        with tempfile.TemporaryDirectory() as directory:
            item = stage('child', target='stage4', root_overlay=[Source.RELENG, Source.PARENT_OVERLAY,
                          Source.STAGE_OVERLAY, '@PROJECT_DIR@/extra'])
            project = FakeProject(directory, [item])
            self.assertEqual(root_overlay_values(project, item), [Source.RELENG, Source.PARENT_OVERLAY,
                             Source.STAGE_OVERLAY, '@PROJECT_DIR@/extra'])

    def test_root_overlay_folders_combine_releng_parent_and_own_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = stage('parent', target='stage4', root_overlay=[Source.STAGE_OVERLAY])
            child = stage('child', target='stage4', parent=parent.id,
                          root_overlay=[Source.RELENG, Source.PARENT_OVERLAY, Source.STAGE_OVERLAY])
            releng = types.SimpleNamespace(directory_path=lambda: '/repo')
            project = FakeProject(directory, [parent, child], releng=releng)
            with patch('catalystlab.project_stage_value_resolver.load_stage_releng_template_values',
                       return_value={StageArgumentDetails.stage4_root_overlay.value: ['@REPO_DIR@/overlays/base']}):
                folders = root_overlay_folders(project, child)
            self.assertEqual(folders, ['/repo/overlays/base', stage_root_overlay_path(project, parent),
                                       stage_root_overlay_path(project, child)])
            self.assertEqual(parent_root_overlay_stages(project, child), [parent])

    def test_root_overlay_options_reject_parent_for_stage3_and_name_iso_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = stage('parent', target='stage3')
            child = stage('child', target='livecd_stage2', parent=parent.id)
            project = FakeProject(directory, [parent, child])
            stage_module = types.ModuleType('catalystlab.project_stage')
            stage_module.parent_supports_argument = lambda *_args: False
            resolver_module = types.ModuleType('catalystlab.project_stage_value_resolver')
            resolver_module.load_stage_releng_template_values = lambda **_kwargs: None
            with patch.dict(sys.modules, {'catalystlab.project_stage': stage_module,
                                          'catalystlab.project_stage_value_resolver': resolver_module}):
                options = root_overlay_options(project, child, StageArgumentDetails.livecd_overlay)
            self.assertIn('ISO overlay defined by releng template', options[0].subtitle)
            self.assertTrue(options[1].unsupported)
            self.assertIn('overlay folder of stage', options[2].subtitle)

    def test_remove_inherited_files_keeps_changed_files_and_removes_empty_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            base, own = Path(directory) / 'base', Path(directory) / 'own'
            (base / 'same').parent.mkdir(parents=True)
            (base / 'same').write_text('identical')
            (base / 'nested').mkdir()
            (base / 'nested' / 'changed').write_text('base')
            (own / 'same').parent.mkdir(parents=True)
            (own / 'same').write_text('identical')
            (own / 'nested').mkdir()
            (own / 'nested' / 'changed').write_text('own')
            (own / 'unique').write_text('unique')
            removed = remove_inherited_files(str(own), [str(base)])
            self.assertEqual(removed, ['same'])
            self.assertFalse((own / 'same').exists())
            self.assertEqual((own / 'nested' / 'changed').read_text(), 'own')
            self.assertEqual((own / 'unique').read_text(), 'unique')

    def test_remove_inherited_files_handles_symlinks_and_empty_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            base, own = Path(directory) / 'base', Path(directory) / 'own'
            base.mkdir(); own.mkdir()
            (base / 'link').symlink_to('target')
            (own / 'link').symlink_to('target')
            (own / 'nested').mkdir()
            self.assertEqual(remove_inherited_files(str(own), [str(base)]), ['link'])
            self.assertFalse(own.exists())
            self.assertEqual(remove_inherited_files(str(own), [str(base)]), [])

    def test_cleanup_removes_duplicate_files_only_from_enabled_parent_overlays(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = stage('parent', target='stage4', portage=[Source.STAGE_OVERLAY], root_overlay=[Source.STAGE_OVERLAY])
            child = stage('child', target='stage4', parent=parent.id,
                          portage=[Source.PARENT_OVERLAY], root_overlay=[Source.PARENT_OVERLAY, Source.STAGE_OVERLAY])
            project = FakeProject(directory, [parent, child])
            for item in (parent, child):
                folder = Path(stage_root_overlay_path(project, item))
                folder.mkdir(parents=True)
                (folder / 'same').write_text('same')
            portage_parent = Path(stage_overlay_path(project, parent)); portage_parent.mkdir(parents=True)
            portage_child = Path(stage_overlay_path(project, child)); portage_child.mkdir(parents=True)
            (portage_parent / 'package.mask').write_text('mask')
            (portage_child / 'package.mask').write_text('mask')
            self.assertEqual(remove_stage_overlays_inherited_files(project, child),
                             {'root_overlay': ['same'], 'portage': ['package.mask']})


if __name__ == '__main__':
    unittest.main()
