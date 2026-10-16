"""Enabling Wi-Fi must pass the row to system validation."""
import ast
from pathlib import Path
import types
import unittest
from unittest.mock import Mock


class WifiEnableCallbackTests(unittest.TestCase):
    def test_enabled_and_disabled_switch_validate_with_row(self):
        path = Path(__file__).resolve().parents[1] / 'src/ui/deploy/deploy_create_view.py'
        tree = ast.parse(path.read_text())
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_on_wifi_enabled')
        namespace = {'threading': Mock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
        for enabled in (False, True):
            row = Mock()
            row.get_enable_expansion.return_value = enabled
            view = types.SimpleNamespace(wifi_row=row, wifi_ssid_row=Mock(),
                _suggest_wifi_name=Mock(), on_system_changed=Mock(side_effect=lambda row: None))
            view.wifi_ssid_row.get_text.return_value = ''
            namespace['_on_wifi_enabled'](view, row, None)
            view.on_system_changed.assert_called_once_with(row)
