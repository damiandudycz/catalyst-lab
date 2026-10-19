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

from catalystlab import project_stage
from catalystlab.project_stage import (
    ProjectStage, default_stage_argument_value, refresh_default_stage_arguments,
    stage_argument_is_customized,
)
from catalystlab.project_stage_arguments import StageArgumentDetails, StageArgumentTargetDetails
from catalystlab.project_stage_automatic_option import StageAutomaticOption
from catalystlab.project_stage_value_resolver import UNRESOLVED
from catalystlab.architecture import Architecture
from catalystlab.project_stage_portage_confdir import StagePortageConfdirSource


class StageDefaultTests(unittest.TestCase):
    def test_template_inheritance_defaults_are_set_even_when_template_value_is_empty(self):
        stage = ProjectStage(name='stage1', target_name='stage1', releng_template_name='template.spec')
        project = types.SimpleNamespace(stages=[])
        arguments = {
            'interpreter': StageArgumentTargetDetails(
                'interpreter', False, StageArgumentDetails.interpreter
            ),
            'compression_mode': StageArgumentTargetDetails(
                'compression_mode', False, StageArgumentDetails.compression_mode
            ),
            'profile': StageArgumentTargetDetails(
                'profile', False, StageArgumentDetails.profile
            ),
        }

        def resolve(_project, _stage, _name, option=None):
            if option == StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE:
                if _name == 'profile':
                    return 'default/linux/arm64/23.0'
                if _name == 'interpreter':
                    return 'qemu-user'
            return UNRESOLVED

        with patch('catalystlab.project_stage.load_catalyst_stage_arguments_details', return_value=arguments), \
             patch('catalystlab.project_stage_value_resolver.resolve_stage_argument', side_effect=resolve):
            applied = refresh_default_stage_arguments(project, stage, toolset=object())

        self.assertEqual(applied, {
            'interpreter': StageAutomaticOption.GENERATE_AUTOMATICALLY,
            'compression_mode': StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE,
            'profile': StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE,
        })
        self.assertEqual(stage.interpreter, StageAutomaticOption.GENERATE_AUTOMATICALLY)
        self.assertEqual(stage.compression_mode, StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE)
        self.assertEqual(stage.profile, StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE)
        self.assertEqual(stage._default_stage_arguments, applied)

    def test_asflags_inherits_from_selected_template_even_when_unset_in_template(self):
        stage = ProjectStage(name='stage1', target_name='stage1', releng_template_name='template.spec')
        project = types.SimpleNamespace(stages=[])
        with patch('catalystlab.project_stage_value_resolver.resolve_stage_argument', return_value=UNRESOLVED):
            self.assertEqual(
                default_stage_argument_value(project, stage, StageArgumentDetails.asflags, object()),
                StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE,
            )

    def test_subarch_uses_releng_value_or_generic_architecture_default(self):
        stage = ProjectStage(name='stage1', target_name='stage1')
        project = types.SimpleNamespace(get_architecture=lambda: Architecture.arm64, stages=[])
        available = [project_stage.CatalystSubarch('arm64', 'aarch64-unknown-linux-gnu', '-O2 -pipe')]
        with patch('catalystlab.project_stage_value_resolver.resolve_stage_argument', return_value=UNRESOLVED), \
             patch('catalystlab.project_stage.parent_supports_argument', return_value=False), \
             patch('catalystlab.project_stage.load_catalyst_subarches', return_value=available):
            self.assertEqual(
                default_stage_argument_value(project, stage, StageArgumentDetails.subarch, object()),
                'arm64',
            )

        stage.releng_template = 'arm64.spec'
        with patch('catalystlab.project_stage_value_resolver.resolve_stage_argument', return_value='aarch64'):
            self.assertEqual(
                default_stage_argument_value(project, stage, StageArgumentDetails.subarch, object()),
                StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE,
            )

    def test_profile_uses_latest_stable_generic_profile_for_project_architecture(self):
        stage = ProjectStage(name='stage1', target_name='stage1')
        profiles = [
            project_stage.PortageProfile('default/linux/amd64/17.1', 'stable', 'gentoo'),
            project_stage.PortageProfile('default/linux/amd64/23.0', 'stable', 'gentoo'),
            project_stage.PortageProfile('default/linux/amd64/23.0/desktop', 'stable', 'gentoo'),
            project_stage.PortageProfile('default/linux/amd64/24.0', 'dev', 'gentoo'),
            project_stage.PortageProfile('default/linux/arm64/23.0', 'stable', 'gentoo'),
        ]
        snapshot = types.SimpleNamespace(load_profiles=lambda arch: profiles)
        project = types.SimpleNamespace(
            stages=[], get_architecture=lambda: Architecture.amd64, get_snapshot=lambda: snapshot
        )
        self.assertEqual(
            default_stage_argument_value(project, stage, StageArgumentDetails.profile, object()),
            profiles[1],
        )

    def test_profile_inherits_only_when_template_has_a_value(self):
        stage = ProjectStage(name='stage1', target_name='stage1', releng_template_name='template.spec')
        project = types.SimpleNamespace(stages=[], get_architecture=lambda: Architecture.amd64,
                                        get_snapshot=lambda: None)
        with patch('catalystlab.project_stage_value_resolver.resolve_stage_argument', return_value=UNRESOLVED):
            self.assertIsNone(default_stage_argument_value(
                project, stage, StageArgumentDetails.profile, object()
            ))
        with patch('catalystlab.project_stage_value_resolver.resolve_stage_argument', return_value='default/linux/amd64/23.0'):
            self.assertEqual(default_stage_argument_value(
                project, stage, StageArgumentDetails.profile, object()
            ), StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE)

    def test_interpreter_stays_automatic_when_template_is_selected(self):
        stage = ProjectStage(name='stage1', target_name='stage1', releng_template_name='template.spec')
        project = types.SimpleNamespace(stages=[])
        self.assertEqual(
            default_stage_argument_value(project, stage, StageArgumentDetails.interpreter, object()),
            StageAutomaticOption.GENERATE_AUTOMATICALLY,
        )

    def test_release_values_fall_back_when_neither_template_nor_parent_is_available(self):
        stage = ProjectStage(name='stage1', target_name='stage1')
        project = types.SimpleNamespace(stages=[])
        self.assertEqual(
            default_stage_argument_value(project, stage, StageArgumentDetails.rel_type, object()),
            'default',
        )
        self.assertEqual(
            default_stage_argument_value(project, stage, StageArgumentDetails.version_stamp, object()),
            '@TIMESTAMP@',
        )

    def test_release_values_still_prefer_available_inheritance(self):
        stage = ProjectStage(name='stage3', target_name='stage3', releng_template_name='release.spec')
        project = types.SimpleNamespace(stages=[])
        for argument in (StageArgumentDetails.rel_type, StageArgumentDetails.version_stamp):
            with self.subTest(argument=argument.name):
                self.assertEqual(
                    default_stage_argument_value(project, stage, argument, object()),
                    StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE,
                )

    def test_every_known_template_inheritable_argument_defaults_to_releng_when_populated(self):
        stage = ProjectStage(name='custom', target_name='custom', releng_template_name='template.spec')
        project = types.SimpleNamespace(stages=[])
        inheritable = [argument for argument in StageArgumentDetails
                       if StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE in argument.automatic_options
                       and argument != StageArgumentDetails.interpreter]

        self.assertTrue(inheritable)
        self.assertTrue(all(argument.name in project_stage.STAGE_DEFAULT_VALUES['ANY']
                            for argument in inheritable))
        with patch('catalystlab.project_stage_value_resolver.resolve_stage_argument', return_value=['template-value']):
            for argument in inheritable:
                with self.subTest(argument=argument.name):
                    self.assertEqual(
                        default_stage_argument_value(project, stage, argument, object()),
                        StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE,
                    )

    def test_refresh_recalculates_old_defaults_and_preserves_explicit_values(self):
        stage = ProjectStage(name='stage1', target_name='stage1')
        stage.parent = 'parent-one'
        stage.asflags = 'old default'
        stage._default_stage_arguments['asflags'] = 'old default'
        stage.profile = 'user profile'
        stage._default_stage_arguments['profile'] = 'stale default'
        stage._explicit_stage_arguments.add('profile')
        stage.interpreter = StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE
        stage._default_stage_arguments['interpreter'] = StageAutomaticOption.GENERATE_AUTOMATICALLY
        stage._explicit_stage_arguments.add('interpreter')
        project = types.SimpleNamespace(stages=[])
        arguments = {
            'asflags': StageArgumentTargetDetails('asflags', False, StageArgumentDetails.asflags),
            'profile': StageArgumentTargetDetails('profile', False, StageArgumentDetails.profile),
            'interpreter': StageArgumentTargetDetails('interpreter', False, StageArgumentDetails.interpreter),
        }
        defaults = {
            'ANY': {
                'asflags': lambda current_stage, *_: f"default for {current_stage.parent}",
                'profile': 'new default',
            }
        }
        with patch.object(project_stage, 'STAGE_DEFAULT_VALUES', defaults), \
             patch('catalystlab.project_stage.load_catalyst_stage_arguments_details', return_value=arguments):
            refreshed = refresh_default_stage_arguments(project, stage, toolset=object())

        self.assertEqual(refreshed, {'asflags': 'default for parent-one'})
        self.assertEqual(stage.asflags, 'default for parent-one')
        self.assertEqual(stage.profile, 'user profile')
        self.assertEqual(stage.interpreter, StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE)
        self.assertEqual(stage._default_stage_arguments, {
            'asflags': 'default for parent-one',
            'profile': 'new default',
            'interpreter': None,
        })

    def test_refresh_never_clears_or_recalculates_stage_identity_fields(self):
        stage = ProjectStage(
            id=__import__('uuid').UUID('18d73835-1bed-4011-aad7-3a14a256e94c'),
            parent_id=None,
            name='stage1',
            target_name='stage1',
            releng_template_name=None,
        )
        stage._default_stage_arguments.update({
            'name': 'stale name default',
            'parent': 'stale parent default',
            'target': 'stale target default',
            'releng_template': 'stale template default',
        })
        arguments = {
            name: StageArgumentTargetDetails(name, False, getattr(StageArgumentDetails, name))
            for name in ('name', 'parent', 'target', 'releng_template')
        }
        with patch('catalystlab.project_stage.load_catalyst_stage_arguments_details', return_value=arguments):
            refresh_default_stage_arguments(types.SimpleNamespace(stages=[]), stage, toolset=object())

        self.assertEqual(str(stage.id), '18d73835-1bed-4011-aad7-3a14a256e94c')
        self.assertIsNone(stage.parent)
        self.assertEqual(stage.name, 'stage1')
        self.assertEqual(stage.target, 'stage1')
        self.assertIsNone(stage.releng_template)
        self.assertFalse(stage._default_stage_arguments)

    def test_recalculated_runtime_default_is_not_customized_against_old_template_baseline(self):
        stage = ProjectStage(name='stage1', target_name='stage1')
        stage.subarch = 'amd64'
        stage._default_stage_arguments['subarch'] = 'amd64'

        self.assertFalse(stage_argument_is_customized(
            stage,
            'subarch',
            set(),
            {'subarch': 'arm64'},
        ))

        # An explicit same-value choice remains customized so it opts out of defaults.
        self.assertTrue(stage_argument_is_customized(
            stage,
            'subarch',
            {'subarch'},
            {'subarch': 'amd64'},
        ))

    def test_template_portage_confdir_matches_source_even_when_runtime_default_differs(self):
        stage = ProjectStage(name='stage1', target_name='stage1')
        stage.portage_confdir = [
            StagePortageConfdirSource.RELENG,
            StagePortageConfdirSource.STAGE_OVERLAY,
        ]
        stage._default_stage_arguments['portage_confdir'] = [StagePortageConfdirSource.RELENG]
        baseline = {
            'portage_confdir': {
                'type': 'list',
                'value': [
                    {'type': 'StagePortageConfdirSource', 'value': 'RELENG'},
                    {'type': 'StagePortageConfdirSource', 'value': 'STAGE_OVERLAY'},
                ],
            },
        }

        self.assertFalse(stage_argument_is_customized(stage, 'portage_confdir', set(), baseline))

        stage.portage_confdir = [StagePortageConfdirSource.RELENG]
        self.assertTrue(stage_argument_is_customized(stage, 'portage_confdir', set(), baseline))

    def test_unsaved_stage_without_source_baseline_is_not_marked_customized(self):
        stage = ProjectStage(name='user-stage', target_name='stage1')
        stage.subarch = 'amd64'

        self.assertFalse(stage_argument_is_customized(stage, 'subarch', set(), {}))

        stage._explicit_user_argument_dates['subarch'] = '2026-10-19T12:00:00Z'
        self.assertTrue(stage_argument_is_customized(stage, 'subarch', set(), {}))

    def test_stage_creation_snapshot_is_the_baseline_for_a_user_added_stage(self):
        stage = ProjectStage(name='user-stage', target_name='stage1')
        stage.subarch = 'amd64'
        stage._default_stage_arguments['subarch'] = 'amd64'
        baseline = {
            'subarch': {'type': 'str', 'value': 'arm64'},
            '__catalystlab_baseline_kind__': 'initial_snapshot',
        }

        self.assertTrue(stage_argument_is_customized(stage, 'subarch', set(), baseline))

        stage.subarch = 'arm64'
        stage._explicit_stage_arguments.add('subarch')
        self.assertFalse(stage_argument_is_customized(stage, 'subarch', set(), baseline))

    def test_creation_wizard_values_marked_explicit_still_match_the_initial_snapshot(self):
        stage = ProjectStage(name='stage1', target_name='stage1')
        baseline = {
            'name': {'type': 'str', 'value': 'stage1'},
            'target': {'type': 'str', 'value': 'stage1'},
            '__catalystlab_baseline_kind__': 'initial_snapshot',
        }

        self.assertFalse(stage_argument_is_customized(stage, 'name', {'name'}, baseline))
        self.assertFalse(stage_argument_is_customized(stage, 'target', {'target'}, baseline))

        stage._explicit_user_argument_dates['target'] = '2026-10-19T12:00:00Z'
        self.assertTrue(stage_argument_is_customized(stage, 'target', {'target'}, baseline))

    def test_timestamp_different_from_template_marks_even_same_value_customized(self):
        stage = ProjectStage(name='stage1', target_name='stage1')
        stage.subarch = 'amd64'
        stage._explicit_user_argument_dates['subarch'] = '2026-10-19T12:00:00Z'
        baseline = {
            'subarch': {
                'type': 'str', 'value': 'amd64',
                'last_changed': '2026-10-19T00:00:00Z',
            },
        }

        self.assertTrue(stage_argument_is_customized(stage, 'subarch', set(), baseline))

        stage._explicit_user_argument_dates['subarch'] = '2026-10-19T00:00:00Z'
        self.assertFalse(stage_argument_is_customized(stage, 'subarch', set(), baseline))

    def test_matching_template_timestamp_ignores_recalculated_default_hidden_by_serialization(self):
        stage = ProjectStage(name='stage1', target_name='stage1')
        stage.common_flags = StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE
        stage._default_stage_arguments['common_flags'] = StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE
        timestamp = '2026-10-19T20:00:00+00:00'
        stage._explicit_user_argument_dates['common_flags'] = timestamp
        baseline = {
            'common_flags': {
                'type': 'NoneType', 'value': None, 'last_changed': timestamp,
            },
        }

        # Runtime defaults are deliberately omitted from the saved stage data.
        self.assertNotIn('common_flags', stage.serialize())
        self.assertFalse(stage_argument_is_customized(stage, 'common_flags', set(), baseline))

    def test_template_values_need_no_template_timestamp_and_local_same_value_edit_is_customized(self):
        stage = ProjectStage(name='stage1', target_name='stage1')
        stage.rel_type = '23.0-default'
        stage._explicit_stage_arguments.add('rel_type')
        stage._default_stage_arguments['rel_type'] = StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE
        baseline = {'rel_type': {'type': 'str', 'value': '23.0-default'}}

        self.assertFalse(stage_argument_is_customized(stage, 'rel_type', set(), baseline))

        # A local timestamp records intent even though the chosen value is equal.
        stage._explicit_user_argument_dates['rel_type'] = '2026-10-19T22:00:00Z'
        self.assertTrue(stage_argument_is_customized(stage, 'rel_type', set(), baseline))

        stage._explicit_user_argument_dates.pop('rel_type')
        stage.rel_type = '23.0'
        self.assertTrue(stage_argument_is_customized(stage, 'rel_type', set(), baseline))

    def test_default_table_prefers_target_value_then_any_and_allows_explicit_none(self):
        stage = ProjectStage(name='stage4', target_name='stage4')
        project = types.SimpleNamespace(name='Demo')
        argument = StageArgumentDetails.profile
        defaults = {
            'ANY': {'profile': 'shared'},
            'stage4': {'profile': lambda selected_stage, selected_project, _toolset:
                       f'{selected_project.name}:{selected_stage.target}'},
            'stage3': {'profile': None},
        }
        with patch.object(project_stage, 'STAGE_DEFAULT_VALUES', defaults):
            self.assertEqual(default_stage_argument_value(project, stage, argument, object()), 'Demo:stage4')
            stage.target = 'stage2'
            self.assertEqual(default_stage_argument_value(project, stage, argument, object()), 'shared')
            defaults['stage4']['profile'] = StageAutomaticOption.GENERATE_AUTOMATICALLY
            stage.target = 'stage4'
            self.assertEqual(default_stage_argument_value(project, stage, argument, object()), 'shared')
            stage.target = 'stage3'
            self.assertIsNone(default_stage_argument_value(project, stage, argument, object()))

    def test_automatic_default_is_ignored_when_argument_does_not_support_it(self):
        stage = ProjectStage(name='stage4', target_name='stage4')
        project = types.SimpleNamespace(stages=[])
        with patch.object(project_stage, 'STAGE_DEFAULT_VALUES', {
            'ANY': {'profile': StageAutomaticOption.GENERATE_AUTOMATICALLY},
        }):
            self.assertIsNone(default_stage_argument_value(
                project, stage, StageArgumentDetails.profile, object()
            ))


if __name__ == '__main__':
    unittest.main()
