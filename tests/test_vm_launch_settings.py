"""Check launch-time resource edits without initializing GTK or starting a VM."""
import ast
import builtins
from pathlib import Path
import types
import unittest
from unittest.mock import Mock


class LaunchSettingsTests(unittest.TestCase):
    def test_launch_overrides_do_not_change_application_settings(self):
        path = Path(__file__).resolve().parents[1] / 'src/ui/qemu/qemu_launch_wizard.py'
        tree = ast.parse(path.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        start = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_start')
        settings = types.SimpleNamespace(qemu_cpus=4, qemu_memory_gib=4)
        repository = types.SimpleNamespace(Settings=types.SimpleNamespace(value=settings, save=Mock()))
        runtime = Mock()
        support = types.SimpleNamespace(available=True, kind='iso')
        namespace = dict(Repository=repository, QemuRun=types.SimpleNamespace(running_runs=[]),
                         qemu_support=Mock(return_value=support), replace=lambda value, **_: value,
                         QemuRunView=runtime, GLib=Mock(),
                         QemuBuildKind=types.SimpleNamespace(STAGE='stage'))
        exec(compile(ast.Module(body=[start], type_ignores=[]), str(path), 'exec'), namespace)
        view = types.SimpleNamespace(backends=[('QEMU', 'qemu')], backend_row=Mock(),
                                     cpus_row=Mock(), memory_row=Mock(), project_directory=object(), build=Mock(),
                                     _launch_started=False, support=types.SimpleNamespace(kind='iso'),
                                     user_rows=[], root_password_entry=Mock(), host_dns_row=Mock(),
                                     host_resolv_conf='nameserver 192.0.2.1\n',
                                     _login_settings_error=lambda: None,
                                     _account_settings_error=lambda: None)
        view.root_password_entry.get_text.return_value = ''
        view.host_dns_row.get_active.return_value = False
        view.kernel_required = False
        view.backend_row.get_selected.return_value = 0
        view.cpus_row.get_value.return_value = 8
        view.memory_row.get_value.return_value = 12
        view.custom_kernel_row = Mock()
        view.custom_kernel_row.get_active.return_value = False
        view._selected_kernel_package = Mock(return_value='sys-kernel/gentoo-kernel')
        namespace['_start'](view, None)
        self.assertEqual(settings.qemu_cpus, 4)
        self.assertEqual(settings.qemu_memory_gib, 4)
        repository.Settings.save.assert_not_called()
        runtime.assert_called_once_with(view.project_directory, view.build, memory_mib=12 * 1024,
                                        cpus=8, support=support, backend='qemu')

    def test_stage_launch_prepares_once_then_starts_vm(self):
        path = Path(__file__).resolve().parents[1] / 'src/ui/qemu/qemu_launch_wizard.py'
        tree = ast.parse(path.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        start = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_start')
        stage_support = types.SimpleNamespace(available=True, kind='stage')
        runtime = Mock()
        preparations = []

        class Preparation:
            def __init__(self, *_):
                preparations.append(self)
                self.on_completed = None
                self.mark_result_seen = Mock()
                self.start = Mock()

        class ProcessView:
            _window = None

            def set_multistage_process(self, process):
                self.process = process

        def test_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == 'multistage_process_execution_view':
                return types.SimpleNamespace(MultistageProcessExecutionView=ProcessView)
            return builtins.__import__(name, globals, locals, fromlist, level)

        events = Mock()
        namespace = dict(
            QemuRun=types.SimpleNamespace(running_runs=[]),
            QemuBuildKind=types.SimpleNamespace(STAGE='stage'),
            qemu_support=Mock(return_value=stage_support),
            apple_support=Mock(),
            replace=lambda value, **changes: types.SimpleNamespace(**vars(value), **changes),
            create_stage_image_directory=Mock(return_value='/tmp/stage-test'),
            QemuRunView=Mock(return_value=runtime), QemuImagePreparation=Preparation,
            MultistageProcessExecutionView=ProcessView,
            AppEvents=types.SimpleNamespace(PRESENT_VIEW='present'), app_event_bus=events,
            GLib=types.SimpleNamespace(idle_add=lambda callback: callback()),
            __builtins__={**vars(builtins), '__import__': test_import},
        )
        exec(compile(ast.Module(body=[start], type_ignores=[]), str(path), 'exec'), namespace)
        view = types.SimpleNamespace(
            backends=[('QEMU', 'qemu')], backend_row=Mock(), cpus_row=Mock(), memory_row=Mock(),
            project_directory=object(), build=Mock(), kernel_required=False, _window=None,
            _launch_started=False,
            custom_kernel_row=Mock(), _selected_kernel_package=Mock(), _selected_snapshot=Mock(),
            _selected_kernel_toolset=Mock(), support=types.SimpleNamespace(kind='stage'), user_rows=[],
            root_password_entry=Mock(), host_dns_row=Mock(), host_resolv_conf='nameserver 192.0.2.1\n',
            _login_settings_error=lambda: None, _account_settings_error=lambda: None,
            auto_close_on_finish=True,
            wizard_view=types.SimpleNamespace(install_view=types.SimpleNamespace(on_finish_requested=None),
                                              set_installation=Mock()),
            _show_runtime=Mock(side_effect=lambda runtime, title: runtime.start()),
        )
        view.root_password_entry.get_text.return_value = ''
        view.host_dns_row.get_active.return_value = False
        view.backend_row.get_selected.return_value = 0
        view.cpus_row.get_value.return_value = 4
        view.memory_row.get_value.return_value = 8
        view.custom_kernel_row.get_active.return_value = True
        view._selected_kernel_package.return_value = 'sys-kernel/gentoo-kernel'
        view._selected_kernel_toolset.return_value = types.SimpleNamespace(busy_reason=None)

        namespace['_start'](view, None)
        namespace['_start'](view, None)
        self.assertEqual(len(preparations), 1)
        runtime.start.assert_not_called()
        preparations[0].on_completed(True)
        runtime.start.assert_called_once_with()
        preparations[0].mark_result_seen.assert_called_once_with()

    def test_stage_without_kernel_cannot_continue_without_custom_kernel(self):
        path = Path(__file__).resolve().parents[1] / 'src/ui/qemu/qemu_launch_wizard.py'
        tree = ast.parse(path.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        ready = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_page_ready')
        qemu_run = types.SimpleNamespace(running_runs=[])
        namespace = {'QemuRun': qemu_run}
        exec(compile(ast.Module(body=[ready], type_ignores=[]), str(path), 'exec'), namespace)
        view = types.SimpleNamespace(backends=[('QEMU', 'qemu')], kernel_required=True,
            _custom_kernel_reason=None, custom_kernel_row=Mock(), _kernel_packages_loading=False,
            _is_custom_kernel_selected=Mock(return_value=False), _valid_custom_kernel=Mock(return_value=True),
            vm_page=object(), kernel_page=object(), accounts_page=object())
        view._selected_kernel_toolset = Mock(return_value=types.SimpleNamespace(busy_reason=None))
        view.custom_kernel_row.get_active.return_value = False
        self.assertFalse(namespace['_page_ready'](view, None, view.kernel_page))
        view.custom_kernel_row.get_active.return_value = True
        self.assertTrue(namespace['_page_ready'](view, None, view.kernel_page))
        view._selected_kernel_toolset.return_value = None
        self.assertFalse(namespace['_page_ready'](view, None, view.kernel_page))
        view._selected_kernel_toolset.return_value = types.SimpleNamespace(busy_reason='in use')
        self.assertFalse(namespace['_page_ready'](view, None, view.kernel_page))
        view._selected_kernel_toolset.return_value = types.SimpleNamespace(busy_reason=None)
        view._custom_kernel_reason = 'Select a project toolset to build a custom kernel.'
        self.assertFalse(namespace['_page_ready'](view, None, view.kernel_page))


if __name__ == '__main__':
    unittest.main()
