"""SlugScript — 安全的 Python 子集解释器，用于市场 slug 过滤。

用户编写 ``def should_include(slug):`` 函数，支持 Python 子集语法：
  - 变量赋值 (=, +=, -=, *=)、if/elif/else、for/while、break/continue、return、pass
  - 字符串方法：startswith/endswith/lower/upper/strip/replace/split/count/find/rfind
  - 运算符：==, !=, <, >, <=, >=, in, not in, and, or, not, +, -, *, %
  - 内置函数：len, str, int, float, bool, abs, min, max, range
  - 下标/切片：s[0], s[1:3], parts[-1]
  - 列表/元组字面量、三元表达式
  - 兼容辅助函数：contains, starts_with, ends_with, contains_any, matches

安全限制：迭代上限 10,000 次/循环，总执行步数上限 50,000。
通过 ast.parse + AST 白名单校验后解释执行，绝不使用 eval / exec。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SLUG_SCRIPT = "def should_include(slug):\n    return True\n"
MAX_SCRIPT_LENGTH = 4_000
MAX_REGEX_LENGTH = 256
MAX_ITERATIONS = 10_000
MAX_TOTAL_STEPS = 50_000

_HELPERS = frozenset({"contains", "starts_with", "ends_with", "contains_any", "matches"})
_STR_METHODS = frozenset({
    "startswith", "endswith", "lower", "upper", "strip", "lstrip", "rstrip",
    "replace", "split", "count", "find", "rfind",
})
_SAFE_BUILTINS = frozenset({"len", "str", "int", "float", "bool", "abs", "min", "max", "range"})
_ALLOWED_CALLS = _HELPERS | _SAFE_BUILTINS
_FORBIDDEN_NAMES = frozenset({
    "exec", "eval", "compile", "open", "input", "print",
    "globals", "locals", "dir", "vars", "getattr", "setattr", "delattr", "hasattr",
    "type", "super", "__import__", "breakpoint", "exit", "quit",
})

_SENTINEL = object()
_BREAK = object()
_CONTINUE = object()


class SlugScriptError(ValueError):
    def __init__(self, message: str, *, line: int | None = None, column: int | None = None) -> None:
        super().__init__(message)
        self.line = line
        self.column = column

    def as_dict(self) -> dict[str, Any]:
        return {"valid": False, "line": self.line, "column": self.column, "message": str(self)}


def _err(node: ast.AST, msg: str) -> SlugScriptError:
    return SlugScriptError(msg, line=getattr(node, "lineno", None), column=getattr(node, "col_offset", 0) + 1)


# ── Validation ──────────────────────────────────────────────

def _validate_stmt(node: ast.stmt) -> None:
    if isinstance(node, ast.Return):
        if node.value is not None:
            _validate_expr(node.value)
        return
    if isinstance(node, ast.If):
        _validate_expr(node.test)
        for s in node.body:
            _validate_stmt(s)
        for s in node.orelse:
            _validate_stmt(s)
        return
    if isinstance(node, ast.Assign):
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise _err(node, "只支持简单变量赋值 (x = ...)")
        name = node.targets[0].id
        if name.startswith("_"):
            raise _err(node, "变量名不能以下划线开头")
        if name in _FORBIDDEN_NAMES:
            raise _err(node, f"不允许使用 {name} 作为变量名")
        _validate_expr(node.value)
        return
    if isinstance(node, ast.For):
        if not isinstance(node.target, ast.Name):
            raise _err(node, "for 循环只支持简单变量 (for x in ...)")
        if node.target.id.startswith("_"):
            raise _err(node, "变量名不能以下划线开头")
        _validate_expr(node.iter)
        for s in node.body:
            _validate_stmt(s)
        for s in node.orelse:
            _validate_stmt(s)
        return
    if isinstance(node, ast.While):
        _validate_expr(node.test)
        for s in node.body:
            _validate_stmt(s)
        for s in node.orelse:
            _validate_stmt(s)
        return
    if isinstance(node, (ast.Break, ast.Continue, ast.Pass)):
        return
    if isinstance(node, ast.Expr):
        _validate_expr(node.value)
        return
    if isinstance(node, ast.AugAssign):
        if not isinstance(node.target, ast.Name):
            raise _err(node, "只支持简单变量的增量赋值")
        if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
            raise _err(node, "增量赋值只支持 +=, -=, *=")
        _validate_expr(node.value)
        return
    raise _err(node, f"不支持的语句类型: {type(node).__name__}")


def _validate_expr(node: ast.expr) -> None:
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (str, int, float, bool, type(None))):
            raise _err(node, f"不支持的常量类型: {type(node.value).__name__}")
        return
    if isinstance(node, ast.Name):
        if node.id in _FORBIDDEN_NAMES:
            raise _err(node, f"不允许使用 {node.id}")
        if node.id.startswith("__"):
            raise _err(node, "不允许使用双下划线名称")
        return
    if isinstance(node, ast.BoolOp):
        for v in node.values:
            _validate_expr(v)
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub)):
        _validate_expr(node.operand)
        return
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Mod)):
        _validate_expr(node.left)
        _validate_expr(node.right)
        return
    if isinstance(node, ast.Compare):
        _validate_expr(node.left)
        for op in node.ops:
            if not isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.Gt, ast.LtE, ast.GtE, ast.In, ast.NotIn)):
                raise _err(node, f"不支持的比较运算符: {type(op).__name__}")
        for c in node.comparators:
            _validate_expr(c)
        return
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            if node.func.id in _FORBIDDEN_NAMES:
                raise _err(node, f"不允许调用 {node.func.id}")
            if node.func.id not in _ALLOWED_CALLS:
                raise _err(node, f"不允许调用 {node.func.id}")
        elif isinstance(node.func, ast.Attribute):
            _validate_expr(node.func.value)
            if node.func.attr.startswith("__"):
                raise _err(node, "不允许调用双下划线方法")
            if node.func.attr not in _STR_METHODS:
                raise _err(node, f"不支持的方法 .{node.func.attr}()，可用: {', '.join(sorted(_STR_METHODS))}")
        else:
            raise _err(node, "不支持的调用方式")
        for a in node.args:
            _validate_expr(a)
        if node.keywords:
            raise _err(node, "不支持关键字参数")
        return
    if isinstance(node, ast.Attribute):
        _validate_expr(node.value)
        if node.attr.startswith("__"):
            raise _err(node, "不允许访问双下划线属性")
        return
    if isinstance(node, ast.Subscript):
        _validate_expr(node.value)
        if isinstance(node.slice, ast.Slice):
            for part in (node.slice.lower, node.slice.upper, node.slice.step):
                if part is not None:
                    _validate_expr(part)
        else:
            _validate_expr(node.slice)
        return
    if isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            _validate_expr(elt)
        return
    if isinstance(node, ast.IfExp):
        _validate_expr(node.test)
        _validate_expr(node.body)
        _validate_expr(node.orelse)
        return
    raise _err(node, f"不支持的表达式: {type(node).__name__}")


def _validate_regex(pattern: str, node: ast.AST) -> None:
    if len(pattern) > MAX_REGEX_LENGTH:
        raise _err(node, f"正则不能超过 {MAX_REGEX_LENGTH} 个字符")
    if "(?" in pattern or re.search(r"\\[1-9]", pattern):
        raise _err(node, "正则不支持 lookaround 或反向引用")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise _err(node, f"正则无效：{exc}") from exc


# ── Evaluation ──────────────────────────────────────────────

class _Evaluator:
    def __init__(self, slug: str) -> None:
        self._vars: dict[str, Any] = {"slug": slug}
        self._total_steps = 0

    def _tick(self) -> None:
        self._total_steps += 1
        if self._total_steps > MAX_TOTAL_STEPS:
            raise SlugScriptError(f"脚本执行步数超过上限 {MAX_TOTAL_STEPS}，可能存在无限循环")

    def run_block(self, stmts: list[ast.stmt]) -> Any:
        for s in stmts:
            r = self._exec_stmt(s)
            if r is not _SENTINEL:
                return r
        return _SENTINEL

    def _exec_stmt(self, node: ast.stmt) -> Any:
        self._tick()
        if isinstance(node, ast.Return):
            return self._eval(node.value) if node.value is not None else None
        if isinstance(node, ast.If):
            branch = node.body if self._eval(node.test) else node.orelse
            return self.run_block(branch)
        if isinstance(node, ast.Assign):
            self._vars[node.targets[0].id] = self._eval(node.value)
            return _SENTINEL
        if isinstance(node, ast.AugAssign):
            name = node.target.id
            old = self._vars.get(name, 0)
            val = self._eval(node.value)
            if isinstance(node.op, ast.Add):
                self._vars[name] = old + val
            elif isinstance(node.op, ast.Sub):
                self._vars[name] = old - val
            elif isinstance(node.op, ast.Mult):
                self._vars[name] = old * val
            return _SENTINEL
        if isinstance(node, ast.For):
            iterable = self._eval(node.iter)
            iterations = 0
            for item in iterable:
                iterations += 1
                if iterations > MAX_ITERATIONS:
                    raise SlugScriptError(f"for 循环超过最大迭代次数 {MAX_ITERATIONS}")
                self._vars[node.target.id] = item
                r = self.run_block(node.body)
                if r is _BREAK:
                    break
                if r is _CONTINUE:
                    continue
                if r is not _SENTINEL:
                    return r
            else:
                r = self.run_block(node.orelse)
                if r is not _SENTINEL and r is not _BREAK and r is not _CONTINUE:
                    return r
            return _SENTINEL
        if isinstance(node, ast.While):
            iterations = 0
            while self._eval(node.test):
                iterations += 1
                if iterations > MAX_ITERATIONS:
                    raise SlugScriptError(f"while 循环超过最大迭代次数 {MAX_ITERATIONS}")
                r = self.run_block(node.body)
                if r is _BREAK:
                    break
                if r is _CONTINUE:
                    continue
                if r is not _SENTINEL:
                    return r
            else:
                r = self.run_block(node.orelse)
                if r is not _SENTINEL and r is not _BREAK and r is not _CONTINUE:
                    return r
            return _SENTINEL
        if isinstance(node, ast.Break):
            return _BREAK
        if isinstance(node, ast.Continue):
            return _CONTINUE
        if isinstance(node, ast.Pass):
            return _SENTINEL
        if isinstance(node, ast.Expr):
            self._eval(node.value)
            return _SENTINEL
        return _SENTINEL

    def _eval(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self._vars:
                return self._vars[node.id]
            if node.id == "True":
                return True
            if node.id == "False":
                return False
            if node.id == "None":
                return None
            raise SlugScriptError(f"未定义的变量: {node.id}", line=node.lineno, column=node.col_offset + 1)

        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result = True
                for v in node.values:
                    result = self._eval(v)
                    if not result:
                        return result
                return result
            else:
                result = False
                for v in node.values:
                    result = self._eval(v)
                    if result:
                        return result
                return result

        if isinstance(node, ast.UnaryOp):
            val = self._eval(node.operand)
            if isinstance(node.op, ast.Not):
                return not val
            if isinstance(node.op, ast.USub):
                return -val

        if isinstance(node, ast.BinOp):
            left = self._eval(node.left)
            right = self._eval(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Mod):
                return left % right

        if isinstance(node, ast.Compare):
            left = self._eval(node.left)
            for op, comp_node in zip(node.ops, node.comparators):
                right = self._eval(comp_node)
                if isinstance(op, ast.Eq):
                    result = left == right
                elif isinstance(op, ast.NotEq):
                    result = left != right
                elif isinstance(op, ast.Lt):
                    result = left < right
                elif isinstance(op, ast.Gt):
                    result = left > right
                elif isinstance(op, ast.LtE):
                    result = left <= right
                elif isinstance(op, ast.GtE):
                    result = left >= right
                elif isinstance(op, ast.In):
                    result = left in right
                elif isinstance(op, ast.NotIn):
                    result = left not in right
                else:
                    raise SlugScriptError("不支持的比较运算符")
                if not result:
                    return False
                left = right
            return True

        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                obj = self._eval(node.func.value)
                method = getattr(obj, node.func.attr)
                args = [self._eval(a) for a in node.args]
                return method(*args)
            if isinstance(node.func, ast.Name):
                name = node.func.id
                args = [self._eval(a) for a in node.args]
                return self._call_func(name, args, node)

        if isinstance(node, ast.Attribute):
            obj = self._eval(node.value)
            return getattr(obj, node.attr)

        if isinstance(node, ast.Subscript):
            obj = self._eval(node.value)
            if isinstance(node.slice, ast.Slice):
                lower = self._eval(node.slice.lower) if node.slice.lower else None
                upper = self._eval(node.slice.upper) if node.slice.upper else None
                step = self._eval(node.slice.step) if node.slice.step else None
                return obj[lower:upper:step]
            idx = self._eval(node.slice)
            return obj[idx]

        if isinstance(node, ast.List):
            return [self._eval(e) for e in node.elts]

        if isinstance(node, ast.Tuple):
            return tuple(self._eval(e) for e in node.elts)

        if isinstance(node, ast.IfExp):
            return self._eval(node.body) if self._eval(node.test) else self._eval(node.orelse)

        raise SlugScriptError("不支持的表达式")

    def _call_func(self, name: str, args: list, node: ast.AST) -> Any:
        if name in _SAFE_BUILTINS:
            builtin = {"len": len, "str": str, "int": int, "float": float,
                        "bool": bool, "abs": abs, "min": min, "max": max,
                        "range": range}[name]
            return builtin(*args)
        if name == "contains":
            return str(args[1]).lower() in str(args[0]).lower()
        if name == "starts_with":
            return str(args[0]).lower().startswith(str(args[1]).lower())
        if name == "ends_with":
            return str(args[0]).lower().endswith(str(args[1]).lower())
        if name == "contains_any":
            s = str(args[0]).lower()
            return any(str(v).lower() in s for v in args[1])
        if name == "matches":
            return re.search(str(args[1]), str(args[0]), re.IGNORECASE) is not None
        raise SlugScriptError(f"未知函数: {name}")


# ── Public API ──────────────────────────────────────────────

@dataclass(frozen=True)
class SlugProgram:
    statements: tuple[ast.stmt, ...]

    @classmethod
    def compile(cls, source: str) -> SlugProgram:
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
            raise _err(func, "函数必须命名为 should_include，且不能使用装饰器")
        if len(func.args.args) != 1 or func.args.args[0].arg != "slug":
            raise _err(func, "函数签名必须是 def should_include(slug):")
        if func.args.vararg or func.args.kwarg or func.args.defaults or func.args.kwonlyargs:
            raise _err(func, "should_include 不能定义额外参数")
        for s in func.body:
            _validate_stmt(s)
        return cls(tuple(func.body))

    def evaluate(self, raw_slug: str) -> bool:
        slug = str(raw_slug or "").strip().lower()
        ev = _Evaluator(slug)
        result = ev.run_block(list(self.statements))
        if result is _SENTINEL:
            raise SlugScriptError("脚本没有返回值，请确保所有路径都有 return")
        return bool(result)


def validate_slug_script(source: str) -> dict[str, Any]:
    try:
        SlugProgram.compile(source)
    except SlugScriptError as exc:
        return exc.as_dict()
    return {"valid": True, "line": None, "column": None, "message": ""}
