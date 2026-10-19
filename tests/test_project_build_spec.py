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
from catalystlab.project_stage import ProjectStage, CatalystSubarch
from catalystlab.architecture import Architecture


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
                   return_value=supported) as load_arguments, patch(
                       "catalystlab.project_stage.refresh_default_stage_arguments"):
            spec = generate_stage_spec(project, stage, StageSpecContext(
                timestamp="20261018T210000Z", source_subpath="seed", portage_confdir=None,
                toolset=toolset, path_mapper=str,
            ))

        load_arguments.assert_called_once_with(toolset=toolset, target_name="stage1")
        self.assertIn("version_stamp: 20261018", spec)
        self.assertNotIn("rename_regexp", spec)

    def test_build_spec_refreshes_defaults_before_reporting_required_values(self):
        toolset = object()
        snapshot = types.SimpleNamespace(filename="gentoo-test.sqfs")
        project = types.SimpleNamespace(get_releng_directory=lambda: None, get_snapshot=lambda: None,
                                        directory_path=lambda: "/project",
                                        stage_directory_path=lambda name: f"/project/stages/{name}")
        stage = types.SimpleNamespace(target="stage1", name="stage1")
        supported = {
            name: StageArgumentTargetDetails(name, True, StageArgumentDetails.named(name))
            for name in ("target", "profile", "subarch", "version_stamp")
        }

        def refresh_defaults(_project, _stage, toolset=None, snapshot=None):
            self.assertIs(toolset, toolset_arg)
            self.assertIs(snapshot, snapshot_arg)
            stage.profile = "default/linux/amd64/23.0"
            stage.subarch = "amd64"
            stage.version_stamp = "@TIMESTAMP@"

        toolset_arg, snapshot_arg = toolset, snapshot
        with patch("catalystlab.project_build_spec.load_catalyst_stage_arguments_details",
                   return_value=supported), patch(
                       "catalystlab.project_stage.refresh_default_stage_arguments", side_effect=refresh_defaults
                   ) as refresh:
            spec = generate_stage_spec(project, stage, StageSpecContext(
                timestamp="20261019T220000Z", source_subpath="seed", portage_confdir=None,
                toolset=toolset, snapshot=snapshot,
            ))

        refresh.assert_called_once_with(project, stage, toolset=toolset, snapshot=snapshot)
        self.assertIn("profile: default/linux/amd64/23.0", spec)
        self.assertIn("subarch: amd64", spec)
        self.assertIn("version_stamp: 20261019T220000Z", spec)

    def test_build_spec_resolves_required_values_from_releng_for_unopened_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec_path = root / "releases" / "specs" / "arm64" / "stage1.spec"
            spec_path.parent.mkdir(parents=True)
            spec_path.write_text(
                "target: stage1\nprofile: default/linux/arm64/23.0\nsubarch: arm64\n"
                "version_stamp: openrc-@TIMESTAMP@\n",
                encoding="utf-8",
            )
            releng = types.SimpleNamespace(directory_path=lambda: str(root))
            project = types.SimpleNamespace(
                get_releng_directory=lambda: releng, get_snapshot=lambda: None,
                get_architecture=lambda: Architecture.arm64, directory_path=lambda: "/project",
                stage_directory_path=lambda name: f"/project/stages/{name}", stages=[],
            )
            stage = ProjectStage(name="stage1", target_name="stage1", releng_template_name="stage1.spec")
            arguments = {
                name: StageArgumentTargetDetails(name, True, StageArgumentDetails.named(name))
                for name in ("target", "profile", "subarch", "version_stamp")
            }
            with patch("catalystlab.project_stage.load_catalyst_stage_arguments_details",
                       return_value=arguments), patch(
                           "catalystlab.project_stage.load_catalyst_subarches",
                           return_value=[CatalystSubarch("arm64", "aarch64-unknown-linux-gnu", None)],
                       ), patch("catalystlab.project_build_spec.load_catalyst_stage_arguments_details",
                                return_value=arguments):
                spec = generate_stage_spec(project, stage, StageSpecContext(
                    timestamp="20261019T220000Z", source_subpath="seed", portage_confdir=None,
                    toolset=object(), path_mapper=str,
                ))

        self.assertIn("profile: default/linux/arm64/23.0", spec)
        self.assertIn("subarch: arm64", spec)
        self.assertIn("version_stamp: openrc-20261019T220000Z", spec)


if __name__ == '__main__':
    unittest.main()
