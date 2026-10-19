"""Catalyst stage-spec value formatting."""
from pathlib import Path
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

from catalystlab.project_build_spec import (
    StageSpecContext, _argument_value, _copy_folders, _format_spec_line, generate_stage_spec, select_seed_url,
)
from catalystlab.project_stage_arguments import StageArgumentDetails, StageArgumentTargetDetails
from catalystlab.project_stage_automatic_option import StageAutomaticOption


class StageSpecFormattingTests(unittest.TestCase):
    def test_interpreter_custom_values_can_be_added_to_automatic_value(self):
        project = types.SimpleNamespace(get_architecture=lambda: None)
        stage = types.SimpleNamespace(interpreter=[StageAutomaticOption.GENERATE_AUTOMATICALLY, "qemu-custom"])
        argument = StageArgumentTargetDetails("interpreter", False, StageArgumentDetails.interpreter)
        context = StageSpecContext(timestamp="now", source_subpath="seed", portage_confdir=None)

        with patch("catalystlab.project_build_spec._automatic_value", return_value="qemu-aarch64"):
            value = _argument_value(project, stage, "interpreter", argument, context)

        self.assertEqual(value, ["qemu-aarch64", "qemu-custom"])

    def test_unset_interpreter_uses_automatic_but_explicit_none_stays_empty(self):
        project = types.SimpleNamespace(get_architecture=lambda: None)
        argument = StageArgumentTargetDetails("interpreter", False, StageArgumentDetails.interpreter)
        context = StageSpecContext(timestamp="now", source_subpath="seed", portage_confdir=None)
        stage = types.SimpleNamespace()

        with patch("catalystlab.project_build_spec._automatic_value", return_value="qemu-aarch64"):
            self.assertEqual(_argument_value(project, stage, "interpreter", argument, context), "qemu-aarch64")
            stage._explicit_none_arguments = {"interpreter"}
            self.assertIsNone(_argument_value(project, stage, "interpreter", argument, context))

    def test_formats_multiline_values_and_replaces_placeholders(self):
        line = _format_spec_line('boot/kernel/linux/packages',
                                 ['@PROJECT_DIR@/kernel', '@TIMESTAMP@'], str,
                                 {'@PROJECT_DIR@': '/project', '@TIMESTAMP@': '20261017'})

        self.assertEqual(line, 'boot/kernel/linux/packages: \n\t/project/kernel\n\t20261017')

    def test_empty_values_are_omitted(self):
        self.assertIsNone(_format_spec_line('stage4/packages', [], str, {}))

    def test_folders_merge_in_order_and_keep_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second, destination = root / 'first', root / 'second', root / 'result'
            first.mkdir(); second.mkdir()
            (first / 'setting').write_text('first')
            (first / 'link').symlink_to('target')
            (second / 'setting').write_text('second')

            _copy_folders([str(first), str(root / 'missing'), str(second)], str(destination))

            self.assertEqual((destination / 'setting').read_text(), 'second')
            self.assertTrue((destination / 'link').is_symlink())

    def test_seed_selection_requires_exact_stage3_timestamped_archive(self):
        urls = [
            types.SimpleNamespace(path="stage3-arm64-openrc-20261017T100000Z.tar.xz"),
            types.SimpleNamespace(path="stage3-arm64-openrc-desktop-20261017T100000Z.tar.xz"),
            types.SimpleNamespace(path="stage3-arm64-openrc-latest.tar.xz"),
        ]

        self.assertIs(urls[0], select_seed_url(urls, "stage3-arm64-openrc"))
        self.assertIsNone(select_seed_url(urls[1:], "stage3-arm64-openrc"))

    def test_build_spec_omits_values_not_supported_by_selected_toolset(self):
        toolset = object()
        project = types.SimpleNamespace(get_releng_directory=lambda: None, get_snapshot=lambda: None,
                                        directory_path=lambda: "/project",
                                        stage_directory_path=lambda name: f"/project/stages/{name}")
        stage = types.SimpleNamespace(target="stage1", name="stage1", version_stamp="20261018",
                                      rename_regexp="s/foo/bar/")
        supported = {
            "target": StageArgumentTargetDetails("target", False, StageArgumentDetails.target),
            "version_stamp": StageArgumentTargetDetails("version_stamp", False, StageArgumentDetails.version_stamp),
        }
        with patch("catalystlab.project_build_spec.load_catalyst_stage_arguments_details",
                   return_value=supported) as load_arguments:
            spec = generate_stage_spec(project, stage, StageSpecContext(
                timestamp="20261018T210000Z", source_subpath="seed", portage_confdir=None,
                toolset=toolset, path_mapper=str,
            ))

        load_arguments.assert_called_once_with(toolset=toolset, target_name="stage1")
        self.assertIn("version_stamp: 20261018", spec)
        self.assertNotIn("rename_regexp", spec)


if __name__ == '__main__':
    unittest.main()
