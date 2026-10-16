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
        namespace = dict(Repository=repository, QemuRun=types.SimpleNamespace(running_runs=[]),
                         qemu_support=Mock(return_value=types.SimpleNamespace(available=True)),
                         QemuRunView=runtime, GLib=Mock())
        exec(compile(ast.Module(body=[start], type_ignores=[]), str(path), 'exec'), namespace)
        view = types.SimpleNamespace(backends=[('QEMU', 'qemu')], backend_row=Mock(),
                                     cpus_row=Mock(), memory_row=Mock(), project_directory=object(), build=Mock())
        view.backend_row.get_selected.return_value = 0
        view.cpus_row.get_value.return_value = 8
        view.memory_row.get_value.return_value = 12
        namespace['_start'](view, None)
        self.assertEqual(settings.qemu_cpus, 4)
        self.assertEqual(settings.qemu_memory_gib, 4)
        repository.Settings.save.assert_not_called()
        runtime.assert_called_once_with(view.project_directory, view.build, memory_mib=12 * 1024,
                                        cpus=8, start_immediately=True, backend='qemu')


if __name__ == '__main__':
    unittest.main()
