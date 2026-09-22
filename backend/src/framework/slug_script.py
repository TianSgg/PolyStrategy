"""SlugScript — 受限 Python 风格的市场 slug 过滤脚本解释器。

用户编写 ``def should_include(slug):`` 函数，仅允许 if/elif/else、return True/False、
以及白名单辅助函数。本模块通过 ast.parse + AST 白名单校验后解释执行，
绝不使用 eval / exec。

移植自 PolyAnalyze replay/market_filter.py，剥离了 PolyAnalyze 特有的
OutcomeResolver / FilteredActivities 等依赖。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

DEFAULT_SLUG_SCRIPT = "def should_include(slug):\n    return True\n"
MAX_SCRIPT_LENGTH = 4_000
MAX_REGEX_LENGTH = 256
_HELPERS = frozenset({"contains", "starts_with", "ends_with", "contains_any", "matches"})


class SlugScriptError(ValueError):
    def __init__(self, message: str, *, line: int | None = None, column: int | None = None) -> None:
        super().__init__(message)
        self.line = line
        self.column = column

    def as_dict(self) -> dict[str, Any]:
        return {"valid": False, "line": self.line, "column": self.column, "message": str(self)}


def _error(node: ast.AST, message: str) -> SlugScriptError:
    return SlugScriptError(message, line=getattr(node, "lineno", None), column=getattr(node, "col_offset", 0) + 1)


def _string(node: ast.AST, *, message: str) -> str:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        raise _error(node, message)
    return node.value


def _validate_regex(pattern: str, node: ast.AST) -> None:
    if len(pattern) > MAX_REGEX_LENGTH:
        raise _error(node, f"正则不能超过 {MAX_REGEX_LENGTH} 个字符")
    if "(?" in pattern or re.search(r"\\[1-9]", pattern):
        raise _error(node, "正则不支持 lookaround 或反向引用")
    if re.search(r"\([^()]*[+*][^()]*\)[+*{]", pattern):
        raise _error(node, "正则不支持嵌套量词")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise _error(node, f"正则无效：{exc}") from exc


def _validate_expression(node: ast.AST) -> None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        for value in node.values:
            _validate_expression(value)
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        _validate_expression(node.operand)
        return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _HELPERS:
        if node.keywords:
            raise _error(node, "过滤函数不支持关键字参数")
        if not node.args or not isinstance(node.args[0], ast.Name) or node.args[0].id != "slug":
            raise _error(node, "过滤函数的第一个参数必须是 slug")
        name = node.func.id
        if name == "contains_any":
            if len(node.args) != 2 or not isinstance(node.args[1], (ast.List, ast.Tuple)):
                raise _error(node, 'contains_any(slug, ["关键词", ...]) 需要字符串列表')
            for value in node.args[1].elts:
                _string(value, message="contains_any 列表只能包含字符串")
            return
        if len(node.args) != 2:
            raise _error(node, f"{name} 需要两个参数")
        value = _string(node.args[1], message=f"{name} 的第二个参数必须是字符串")
        if name == "matches":
            _validate_regex(value, node.args[1])
        return
    raise _error(node, "只允许布尔逻辑和 contains / starts_with / ends_with / contains_any / matches")


def _validate_block(statements: list[ast.stmt]) -> None:
    if not statements:
        raise SlugScriptError("每个分支必须返回 True 或 False")
    for statement in statements:
        if isinstance(statement, ast.Return):
            _validate_expression(statement.value)
        elif isinstance(statement, ast.If):
            _validate_expression(statement.test)
            _validate_block(statement.body)
            if statement.orelse:
                _validate_block(statement.orelse)
        else:
            raise _error(statement, "只允许 if / elif / else 和 return")


def _guarantees_return(statements: Iterable[ast.stmt]) -> bool:
    for statement in statements:
        if isinstance(statement, ast.Return):
            return True
        if isinstance(statement, ast.If) and statement.orelse:
            if _guarantees_return(statement.body) and _guarantees_return(statement.orelse):
                return True
    return False


@dataclass(frozen=True)
class SlugProgram:
    statements: tuple[ast.stmt, ...]

    @classmethod
    def compile(cls, source: str) -> "SlugProgram":
        if len(source) > MAX_SCRIPT_LENGTH:
            raise SlugScriptError(f"脚本不能超过 {MAX_SCRIPT_LENGTH} 个字符")
        try:
            tree = ast.parse(source, mode="exec")
        except SyntaxError as exc:
            raise SlugScriptError(exc.msg, line=exc.lineno, column=exc.offset) from exc
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
            raise SlugScriptError("脚本必须只包含 def should_include(slug): 函数")
        func = tree.body[0]
        if func.name != "should_include" or func.decorator_list:
            raise _error(func, "函数必须命名为 should_include，且不能使用装饰器")
        if len(func.args.args) != 1 or func.args.args[0].arg != "slug":
            raise _error(func, "函数签名必须是 def should_include(slug):")
        if func.args.vararg or func.args.kwarg or func.args.defaults or func.args.kwonlyargs:
            raise _error(func, "should_include 不能定义额外参数")
        _validate_block(func.body)
        if not _guarantees_return(func.body):
            raise _error(func, "所有执行路径都必须 return True 或 False")
        return cls(tuple(func.body))

    def evaluate(self, raw_slug: str) -> bool:
        slug = str(raw_slug or "").strip().lower()
        value = self._run_block(self.statements, slug)
        if value is None:
            raise SlugScriptError("所有执行路径都必须 return True 或 False")
        return value

    def _run_block(self, statements: Iterable[ast.stmt], slug: str) -> bool | None:
        for statement in statements:
            if isinstance(statement, ast.Return):
                return bool(self._eval_expression(statement.value, slug))
            if isinstance(statement, ast.If):
                branch = statement.body if self._eval_expression(statement.test, slug) else statement.orelse
                result = self._run_block(branch, slug)
                if result is not None:
                    return result
        return None

    def _eval_expression(self, node: ast.AST, slug: str) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BoolOp):
            values = (self._eval_expression(value, slug) for value in node.values)
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not self._eval_expression(node.operand, slug)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name == "contains_any":
                values = [_string(item, message="contains_any 列表只能包含字符串").lower() for item in node.args[1].elts]
                return any(value in slug for value in values)
            text_value = _string(node.args[1], message=f"{name} 的第二个参数必须是字符串").lower()
            if name == "contains":
                return text_value in slug
            if name == "starts_with":
                return slug.startswith(text_value)
            if name == "ends_with":
                return slug.endswith(text_value)
            if name == "matches":
                return re.search(text_value, slug) is not None
        raise SlugScriptError("脚本包含未验证表达式")


def validate_slug_script(source: str) -> dict[str, Any]:
    try:
        SlugProgram.compile(source)
    except SlugScriptError as exc:
        return exc.as_dict()
    return {"valid": True, "line": None, "column": None, "message": ""}
