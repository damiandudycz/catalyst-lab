"""AVF allocations must never be presented as live CPU/RSS measurements."""
import ast
from pathlib import Path
import types
import unittest
from unittest.mock import Mock


class VMUsageTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'src/ui/qemu/qemu_run_view.py'
        tree = ast.parse(path.read_text())
        view = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'QemuRunView')
        methods = [n for n in view.body if isinstance(n, ast.FunctionDef) and n.name in ('_show_usage', '_clear_usage')]
        namespace = dict(QemuBuildKind=types.SimpleNamespace(STAGE='stage'), _gib=lambda size: f'{size / 1024 ** 3:.1f} GiB')
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)] + methods, type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
        self.show = namespace['_show_usage']
        self.clear = namespace['_clear_usage']
        self.run = types.SimpleNamespace(is_running=True, cpus=8, memory_mib=12 * 1024)
        self.view = types.SimpleNamespace(run=self.run, backend='apple', support=types.SimpleNamespace(kind='stage'),
            usage_group=Mock(), usage_cpu_row=types.SimpleNamespace(set_subtitle=Mock()),
            usage_memory_row=types.SimpleNamespace(set_subtitle=Mock()), usage_disk_row=Mock())

    def test_avf_keeps_allocations_and_updates_disk_without_cpu_sample(self):
        self.show(self.view, self.run, None, (1 << 30, 4 << 30))
        self.view.usage_cpu_row.set_subtitle.assert_called_once_with('8 vCPUs')
        self.view.usage_memory_row.set_subtitle.assert_called_once_with('12 GiB')
        self.view.usage_disk_row.graph.add.assert_called_once_with(0.25)
        self.view.usage_group.set_visible.assert_called_with(True)

    def test_clearing_avf_usage_does_not_clear_allocation_labels(self):
        self.clear(self.view)
        self.view.usage_cpu_row.set_subtitle.assert_not_called()
        self.view.usage_memory_row.set_subtitle.assert_not_called()
        self.view.usage_disk_row.graph.clear.assert_called_once()

    def test_qemu_still_shows_live_measurements(self):
        self.view.backend = 'qemu'
        self.view.usage_cpu_row = Mock()
        self.view.usage_memory_row = Mock()
        self.show(self.view, self.run, (0.5, 3 << 30), None)
        self.view.usage_cpu_row.graph.add.assert_called_once_with(0.5)
        self.view.usage_memory_row.graph.add.assert_called_once_with(0.25)
        self.view.usage_cpu_row.set_subtitle.assert_called_once_with('50% of 8 vCPUs')


if __name__ == '__main__':
    unittest.main()
