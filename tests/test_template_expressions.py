"""The project-template expression language stays useful without executing Python code."""
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab.project_template import TemplateError, TemplateExpression, evaluate_condition, render_value


class TemplateExpressionTests(unittest.TestCase):
    def test_values_operators_and_short_circuiting(self):
        self.assertEqual(TemplateExpression("name + '-' + upper(arch)").evaluate({'name': 'test', 'arch': 'arm64'}), 'test-ARM64')
        self.assertEqual(TemplateExpression("['a'] + ['b']").evaluate({}), ['a', 'b'])
        self.assertEqual(TemplateExpression('not enabled or count >= 2').evaluate({'enabled': False, 'count': 2}), True)
        self.assertEqual(TemplateExpression("'arm64' if arch == 'arm64' else 'x86_64'").evaluate({'arch': 'arm64'}), 'arm64')
        self.assertEqual(TemplateExpression("join('-', ['a', 'b'])").evaluate({}), 'a-b')

    def test_conditions_default_to_true_and_evaluate_boolean_values(self):
        self.assertTrue(evaluate_condition(None, {}))
        self.assertTrue(evaluate_condition("arch in ['arm64', 'x86_64']", {'arch': 'arm64'}))
        self.assertFalse(evaluate_condition('enabled', {'enabled': False}))

    def test_rendering_preserves_typed_full_expression_and_renders_nested_values(self):
        names = {'count': 4, 'arch': 'arm64'}
        self.assertEqual(render_value('{{ count + 1 }}', names), 5)
        self.assertEqual(render_value('Build {{ upper(arch) }} #{{ count }}', names), 'Build ARM64 #4')
        self.assertEqual(render_value({'items': ['{{ count }}', 'arch={{ arch }}']}, names),
                         {'items': [4, 'arch=arm64']})
        self.assertEqual(render_value('literal {{ missing if False else "" }}', {}), 'literal ')

    def test_rejects_unknown_names_and_python_execution_constructs(self):
        for expression in ('missing', 'open("/etc/passwd")', 'arch.upper()', '[x for x in []]', '__import__("os")'):
            with self.subTest(expression=expression):
                with self.assertRaises(TemplateError):
                    TemplateExpression(expression).evaluate({'arch': 'arm64'})

    def test_rejects_invalid_argument_types_and_mixed_type_addition(self):
        for expression in ('lower(1)', "'text' + 1", '1 - true', '1 < "a"'):
            with self.subTest(expression=expression):
                with self.assertRaises(TemplateError):
                    TemplateExpression(expression).evaluate({})

    def test_expression_length_and_rendered_string_length_are_bounded(self):
        with self.assertRaisesRegex(TemplateError, 'too long'):
            TemplateExpression("'" + 'x' * 2_001 + "'")
        with self.assertRaisesRegex(TemplateError, 'too long'):
            render_value('{{ upper(value) }}', {'value': 'x' * 100_001})


if __name__ == '__main__':
    unittest.main()
