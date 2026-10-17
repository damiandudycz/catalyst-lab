"""Check launch-time resource edits without initializing GTK or starting a VM."""
import ast
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
                                     cpus_row=Mock(), memory_row=Mock(), project_directory=object(), build=Mock())
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
                                        cpus=8, support=support, start_immediately=True, backend='qemu')

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
            _is_custom_kernel_selected=Mock(return_value=False), _valid_custom_kernel=Mock(return_value=True))
        view.custom_kernel_row.get_active.return_value = False
        self.assertFalse(namespace['_page_ready'](view, None, None))
        view.custom_kernel_row.get_active.return_value = True
        self.assertTrue(namespace['_page_ready'](view, None, None))
        view._custom_kernel_reason = 'Select a project toolset to build a custom kernel.'
        self.assertFalse(namespace['_page_ready'](view, None, None))


if __name__ == '__main__':
    unittest.main()
