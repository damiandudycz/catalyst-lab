"""Safe expression language used by project templates."""
from __future__ import annotations

import ast
import re
from typing import Any, Callable


class TemplateError(Exception):
    """Invalid or unsafe template definition or expression."""


_MAX_EXPRESSION_LENGTH = 2_000
_MAX_EVALUATION_STEPS = 10_000
_MAX_STRING_LENGTH = 100_000
_MAX_LIST_LENGTH = 10_000
_PLACEHOLDER_PATTERN = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


def _string_argument(function_name: str, value) -> str:
    if not isinstance(value, str):
        raise TemplateError(f"{function_name}() expects text, got {type(value).__name__}")
    return value


_FUNCTIONS: dict[str, Callable] = {
    "lower": lambda text: _string_argument("lower", text).lower(),
    "upper": lambda text: _string_argument("upper", text).upper(),
    "replace": lambda text, old, new: _string_argument("replace", text).replace(
        _string_argument("replace", old), _string_argument("replace", new)),
    "join": lambda separator, items: _string_argument("join", separator).join(
        _string_argument("join", item) for item in items),
}
EXPRESSION_FUNCTION_NAMES = frozenset(_FUNCTIONS)


class TemplateExpression:
    """Evaluate a safe subset of Python syntax without executing template code."""

    def __init__(self, source: str):
        if not isinstance(source, str):
            raise TemplateError(f"Expression must be text: {source!r}")
        source = source.strip()
        if len(source) > _MAX_EXPRESSION_LENGTH:
            raise TemplateError(f"Expression is too long: {source[:50]}...")
        try:
            self.tree = ast.parse(source, mode="eval")
        except SyntaxError as error:
            raise TemplateError(f"Invalid expression '{source}': {error.msg}") from None
        self.source = source

    def evaluate(self, names: dict[str, Any]) -> Any:
        self._steps = 0
        self._names = names
        try:
            return self._evaluate(self.tree.body)
        except TemplateError as error:
            raise TemplateError(f"{error} (in '{self.source}')") from None
        except (TypeError, ValueError) as error:
            raise TemplateError(f"Failed to evaluate '{self.source}': {error}") from None

    def _checked(self, value):
        if isinstance(value, str) and len(value) > _MAX_STRING_LENGTH:
            raise TemplateError("Text value is too long")
        if isinstance(value, list) and len(value) > _MAX_LIST_LENGTH:
            raise TemplateError("List value is too long")
        return value

    def _evaluate(self, node: ast.AST):
        self._steps += 1
        if self._steps > _MAX_EVALUATION_STEPS:
            raise TemplateError("Expression is too complex")
        match node:
            case ast.Constant(value=value) if value is None or isinstance(value, (str, int, float, bool)):
                return value
            case ast.Name(id=name):
                if name not in self._names:
                    raise TemplateError(f"Unknown name '{name}'")
                return self._checked(self._names[name])
            case ast.List(elts=elements) | ast.Tuple(elts=elements):
                return self._checked([self._evaluate(element) for element in elements])
            case ast.BoolOp(op=op, values=values):
                result = None
                for value in values:
                    result = self._evaluate(value)
                    if isinstance(op, ast.And) and not result:
                        return result
                    if isinstance(op, ast.Or) and result:
                        return result
                return result
            case ast.UnaryOp(op=ast.Not(), operand=operand):
                return not self._evaluate(operand)
            case ast.UnaryOp(op=ast.USub(), operand=operand):
                value = self._evaluate(operand)
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise TemplateError("Minus can be used only with numbers")
                return -value
            case ast.BinOp(left=left, op=ast.Add() | ast.Sub() as op, right=right):
                left_value, right_value = self._evaluate(left), self._evaluate(right)
                numbers = all(isinstance(value, (int, float)) and not isinstance(value, bool)
                              for value in (left_value, right_value))
                if isinstance(op, ast.Sub):
                    if not numbers:
                        raise TemplateError("Minus can be used only with numbers")
                    return left_value - right_value
                if not numbers and not (type(left_value) is type(right_value) and isinstance(left_value, (str, list))):
                    raise TemplateError(f"Can't add {type(left_value).__name__} and {type(right_value).__name__}")
                return self._checked(left_value + right_value)
            case ast.Compare(left=left, ops=ops, comparators=comparators):
                left_value = self._evaluate(left)
                for op, comparator in zip(ops, comparators):
                    right_value = self._evaluate(comparator)
                    match op:
                        case ast.Eq(): result = left_value == right_value
                        case ast.NotEq(): result = left_value != right_value
                        case ast.Lt(): result = left_value < right_value
                        case ast.LtE(): result = left_value <= right_value
                        case ast.Gt(): result = left_value > right_value
                        case ast.GtE(): result = left_value >= right_value
                        case ast.In() | ast.NotIn():
                            if not isinstance(right_value, (str, list)):
                                raise TemplateError("'in' can be used only with text and lists")
                            result = (left_value in right_value) == isinstance(op, ast.In)
                        case _:
                            raise TemplateError(f"Comparison {type(op).__name__} is not allowed")
                    if not result:
                        return False
                    left_value = right_value
                return True
            case ast.IfExp(test=test, body=body, orelse=orelse):
                return self._evaluate(body) if self._evaluate(test) else self._evaluate(orelse)
            case ast.JoinedStr(values=values):
                return self._checked("".join(str(self._evaluate(value)) for value in values))
            case ast.FormattedValue(value=value, conversion=-1, format_spec=None):
                return self._evaluate(value)
            case ast.Call(func=ast.Name(id=name), args=args, keywords=[]) if name in _FUNCTIONS and not any(
                    isinstance(arg, ast.Starred) for arg in args):
                return self._checked(_FUNCTIONS[name](*[self._evaluate(arg) for arg in args]))
        raise TemplateError(f"{type(node).__name__} is not allowed in template expressions")


def evaluate_condition(condition: str | None, names: dict[str, Any]) -> bool:
    """Evaluate a template ``when`` condition; an omitted condition is true."""
    return True if condition is None else bool(TemplateExpression(condition).evaluate(names))


def render_value(value, names: dict[str, Any]):
    """Render placeholders recursively, preserving the type of a full-expression value."""
    if isinstance(value, str):
        placeholders = list(_PLACEHOLDER_PATTERN.finditer(value))
        if len(placeholders) == 1 and placeholders[0].span() == (0, len(value)):
            return TemplateExpression(placeholders[0].group(1)).evaluate(names)

        def replace(match: re.Match) -> str:
            result = TemplateExpression(match.group(1)).evaluate(names)
            return "" if result is None else str(result)

        rendered = _PLACEHOLDER_PATTERN.sub(replace, value)
        if len(rendered) > _MAX_STRING_LENGTH:
            raise TemplateError("Text value is too long")
        return rendered
    if isinstance(value, list):
        return [render_value(item, names) for item in value]
    if isinstance(value, dict):
        return {key: render_value(item, names) for key, item in value.items()}
    return value
