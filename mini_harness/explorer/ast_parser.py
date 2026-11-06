"""Python 语言适配器：基于标准库 ``ast`` 的静态结构与调用收集。

Phase 2 只提取“确定性的结构事实”：符号、行号、签名、import 和原始调用
表达式。跨文件调用解析、继承关系推断和 CodeGraph 构建属于 Phase 3。
"""

from __future__ import annotations

import ast
from pathlib import PurePosixPath

from .base import LanguageAdapter
from .models import (
    CallRecord,
    DiagnosticRecord,
    ImportRecord,
    ParsedFile,
    SourceFile,
    SymbolRecord,
)


def module_name_from_relative_path(relative_path: str) -> str:
    """把 ``pkg/mod.py`` 或 ``pkg/__init__.py`` 转换为模块名。

    Examples:
        ``mini_harness/workspace.py`` -> ``mini_harness.workspace``
        ``mini_harness/__init__.py`` -> ``mini_harness``
    """
    path = PurePosixPath(relative_path)
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return path.stem
    return ".".join(parts)


def _safe_unparse(node: ast.AST) -> str:
    """``ast.unparse`` 的安全版本；失败时返回空字符串而不是中断扫描。"""
    try:
        return ast.unparse(node)
    except (AttributeError, TypeError, ValueError):
        return ""


def _call_name(node: ast.AST) -> str:
    """提取调用的可读名称，例如 ``foo`` 或 ``self.foo``。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        current: ast.AST = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
    return _safe_unparse(node)


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """生成包含 async、参数和返回注解的签名。"""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    signature = f"{prefix} {node.name}({_safe_unparse(node.args)})"
    if node.returns is not None:
        signature += f" -> {_safe_unparse(node.returns)}"
    return signature


def _class_signature(node: ast.ClassDef) -> str:
    """生成类签名；只包含直接基类，不推断继承关系。"""
    bases = [_safe_unparse(base) for base in node.bases]
    if bases:
        return f"class {node.name}({', '.join(bases)})"
    return f"class {node.name}"


class _PythonVisitor(ast.NodeVisitor):
    """遍历 AST 并维护当前符号作用域。"""

    def __init__(self, module_symbol: SymbolRecord) -> None:
        self.module_symbol = module_symbol
        self.symbols: list[SymbolRecord] = [module_symbol]
        self.imports: list[ImportRecord] = []
        self.calls: list[CallRecord] = []
        self.diagnostics: list[DiagnosticRecord] = []
        self._scope: list[SymbolRecord] = [module_symbol]

    @property
    def current_scope(self) -> SymbolRecord:
        """当前正在访问的最近符号。"""
        return self._scope[-1]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """记录类，并把类体中的函数标记为方法。"""
        parent = self.current_scope
        qualified_name = f"{parent.qualified_name}.{node.name}"
        symbol = SymbolRecord(
            id=f"class:{qualified_name}",
            kind="class",
            name=node.name,
            qualified_name=qualified_name,
            file_path=self.module_symbol.file_path,
            start_line=node.lineno,
            end_line=getattr(node, "end_lineno", node.lineno),
            signature=_class_signature(node),
            docstring=ast.get_docstring(node, clean=True),
            decorators=[
                _safe_unparse(item) for item in node.decorator_list
            ],
            bases=[_safe_unparse(base) for base in node.bases],
            parent_id=parent.id,
            language=self.module_symbol.language,
        )
        self.symbols.append(symbol)

        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)

        self._scope.append(symbol)
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """记录函数或方法，并在其作用域内收集调用。"""
        parent = self.current_scope
        kind = "method" if parent.kind == "class" else "function"
        qualified_name = f"{parent.qualified_name}.{node.name}"
        symbol = SymbolRecord(
            id=f"{kind}:{qualified_name}",
            kind=kind,
            name=node.name,
            qualified_name=qualified_name,
            file_path=self.module_symbol.file_path,
            start_line=node.lineno,
            end_line=getattr(node, "end_lineno", node.lineno),
            signature=_function_signature(node),
            docstring=ast.get_docstring(node, clean=True),
            decorators=[
                _safe_unparse(item) for item in node.decorator_list
            ],
            parent_id=parent.id,
            language=self.module_symbol.language,
        )
        self.symbols.append(symbol)

        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)

        self._scope.append(symbol)
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self._scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """异步函数与普通函数共用提取逻辑。"""
        self.visit_FunctionDef(node)

    def visit_Import(self, node: ast.Import) -> None:
        """记录 ``import x`` / ``import x as y``。"""
        for alias in node.names:
            self.imports.append(
                ImportRecord(
                    file_path=self.module_symbol.file_path,
                    module=alias.name,
                    name=None,
                    alias=alias.asname,
                    level=0,
                    line=node.lineno,
                    column=node.col_offset,
                )
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """记录 ``from x import y``，保留相对导入层级。"""
        for alias in node.names:
            self.imports.append(
                ImportRecord(
                    file_path=self.module_symbol.file_path,
                    module=node.module or "",
                    name=alias.name,
                    alias=alias.asname,
                    level=node.level,
                    line=node.lineno,
                    column=node.col_offset,
                )
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """记录原始调用表达式，不在此阶段做跨文件解析。"""
        self.calls.append(
            CallRecord(
                caller_id=self.current_scope.id,
                callee_expression=_safe_unparse(node.func),
                callee_name=_call_name(node.func),
                line=node.lineno,
                column=node.col_offset,
            )
        )
        self.generic_visit(node)


class PythonAstParser(LanguageAdapter):
    """使用 Python 标准库 ``ast`` 的语言适配器。"""

    language = "python"
    extensions = (".py",)

    def parse(self, source: SourceFile) -> ParsedFile:
        """解析单个 Python 文件，语法错误以诊断形式返回。"""
        try:
            tree = ast.parse(source.text, filename=source.relative_path)
        except SyntaxError as exc:
            return ParsedFile(
                diagnostics=[
                    DiagnosticRecord(
                        code="syntax_error",
                        message=str(exc),
                        severity="error",
                        file_path=source.relative_path,
                        line=exc.lineno,
                        column=exc.offset,
                    )
                ]
            )
        except (TypeError, ValueError) as exc:
            return ParsedFile(
                diagnostics=[
                    DiagnosticRecord(
                        code="parse_error",
                        message=str(exc),
                        severity="error",
                        file_path=source.relative_path,
                    )
                ]
            )

        module_name = module_name_from_relative_path(source.relative_path)
        module_symbol = SymbolRecord(
            id=f"module:{source.relative_path}",
            kind="module",
            name=module_name,
            qualified_name=module_name,
            file_path=source.relative_path,
            start_line=1,
            end_line=max(1, source.line_count),
            docstring=ast.get_docstring(tree, clean=True),
            parent_id=None,
            language=self.language,
        )

        visitor = _PythonVisitor(module_symbol)
        try:
            for statement in tree.body:
                visitor.visit(statement)
        except Exception as exc:  # noqa: BLE001 - 单文件失败不能中断整个项目扫描
            visitor.diagnostics = [
                DiagnosticRecord(
                    code="ast_walk_error",
                    message=str(exc),
                    severity="error",
                    file_path=source.relative_path,
                )
            ]
        else:
            visitor.diagnostics = []

        return ParsedFile(
            symbols=visitor.symbols,
            imports=visitor.imports,
            calls=visitor.calls,
            diagnostics=visitor.diagnostics,
        )


__all__ = ["PythonAstParser", "module_name_from_relative_path"]
