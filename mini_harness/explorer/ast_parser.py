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


_LITERAL_BUILTIN_TYPES = {
    str: "str",
    bytes: "bytes",
    int: "int",
    float: "float",
    bool: "bool",
}


def _extract_type_name(annotation: ast.AST | None) -> str | None:
    """从 Python 类型注解 AST 中提取主类型名（支持 Optional / Union / A | None / 字符串前向引用）。"""
    if annotation is None:
        return None
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return _call_name(annotation)
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        raw = annotation.value.strip()
        if not raw:
            return None
        try:
            parsed = ast.parse(raw, mode="eval").body
        except SyntaxError:
            return raw.split("[", 1)[0].split("|", 1)[0].strip() or None
        return _extract_type_name(parsed)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        left = _extract_type_name(annotation.left)
        right = _extract_type_name(annotation.right)
        if left and left != "None":
            return left
        if right and right != "None":
            return right
        return None
    if isinstance(annotation, ast.Subscript):
        base = _extract_type_name(annotation.value)
        if base in ("Optional", "Union", "ClassVar", "Final", "Annotated"):
            slice_node = annotation.slice
            if isinstance(slice_node, ast.Tuple):
                for elt in slice_node.elts:
                    cand = _extract_type_name(elt)
                    if cand and cand != "None":
                        return cand
                return None
            return _extract_type_name(slice_node)
        return base
    return None


def _infer_value_type(
    value: ast.AST | None,
    var_types: list[dict[str, str]],
) -> str | None:
    """仅从可靠静态线索推导右值类型（字面量、已知变量、直接大写类构造）。

    注意：故意不推导小写函数调用（如 ``make_service()``），避免臆测动态返回类型。
    """
    if value is None:
        return None
    if isinstance(value, ast.Constant):
        return _LITERAL_BUILTIN_TYPES.get(type(value.value))
    if isinstance(value, ast.JoinedStr):
        return "str"
    if isinstance(value, (ast.List, ast.ListComp)):
        return "list"
    if isinstance(value, (ast.Dict, ast.DictComp)):
        return "dict"
    if isinstance(value, (ast.Set, ast.SetComp)):
        return "set"
    if isinstance(value, ast.Tuple):
        return "tuple"
    if isinstance(value, ast.Name):
        for scope in reversed(var_types):
            if value.id in scope:
                return scope[value.id]
        return None
    if isinstance(value, ast.Call):
        callee = _call_name(value.func)
        if not callee:
            return None
        last_part = callee.rsplit(".", 1)[-1]
        if last_part in ("list", "dict", "set", "tuple", "str", "bytes"):
            return last_part
        # 直接类构造（如 MemoryStore(...)、pkg.SessionManager(...)），跳过普通小写函数调用
        if last_part and last_part[0].isupper() and not last_part.isupper():
            return callee
    return None


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
    """遍历 AST 并维护当前符号作用域与可靠静态类型绑定。"""

    def __init__(self, module_symbol: SymbolRecord) -> None:
        self.module_symbol = module_symbol
        self.symbols: list[SymbolRecord] = [module_symbol]
        self.imports: list[ImportRecord] = []
        self.calls: list[CallRecord] = []
        self.diagnostics: list[DiagnosticRecord] = []
        self._scope: list[SymbolRecord] = [module_symbol]
        self._var_types: list[dict[str, str]] = [{}]
        self._class_attr_types: list[dict[str, str]] = []
        self._used_ids: set[str] = {module_symbol.id}

    def _lookup_var_type(self, name: str) -> str | None:
        for table in reversed(self._var_types):
            found = table.get(name)
            if found is not None:
                return found
        return None

    def _lookup_self_attr_type(self, attr: str) -> str | None:
        if not self._class_attr_types:
            return None
        return self._class_attr_types[-1].get(attr)

    def _collect_class_attr_types(self, node: ast.ClassDef) -> dict[str, str]:
        """第一遍扫描类属性注解与 __init__ 中的 self.x 类型，供所有方法复用。"""
        attrs: dict[str, str] = {}
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                t = _extract_type_name(stmt.annotation)
                if t:
                    attrs[stmt.target.id] = t
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == "__init__":
                init_params: dict[str, str] = {}
                for arg in (*stmt.args.posonlyargs, *stmt.args.args, *stmt.args.kwonlyargs):
                    t = _extract_type_name(arg.annotation)
                    if t:
                        init_params[arg.arg] = t
                for sub in ast.walk(stmt):
                    if (
                        isinstance(sub, ast.AnnAssign)
                        and isinstance(sub.target, ast.Attribute)
                        and isinstance(sub.target.value, ast.Name)
                        and sub.target.value.id == "self"
                    ):
                        t = _extract_type_name(sub.annotation) or _infer_value_type(
                            sub.value, [init_params]
                        )
                        if t:
                            attrs[sub.target.attr] = t
                    elif isinstance(sub, ast.Assign):
                        inferred = _infer_value_type(sub.value, [init_params])
                        if not inferred:
                            continue
                        for target in sub.targets:
                            if (
                                isinstance(target, ast.Attribute)
                                and isinstance(target.value, ast.Name)
                                and target.value.id == "self"
                            ):
                                attrs.setdefault(target.attr, inferred)
        return attrs

    @property
    def current_scope(self) -> SymbolRecord:
        """当前正在访问的最近符号。"""
        return self._scope[-1]

    def _make_id(self, base: str, line: int) -> str:
        """生成文件内唯一 ID；同名 property getter/setter 等需加行号区分。"""
        if base not in self._used_ids:
            self._used_ids.add(base)
            return base
        candidate = f"{base}@{line}"
        suffix = 2
        while candidate in self._used_ids:
            candidate = f"{base}@{line}-{suffix}"
            suffix += 1
        self._used_ids.add(candidate)
        return candidate

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """记录类，并把类体中的函数标记为方法。"""
        parent = self.current_scope
        qualified_name = f"{parent.qualified_name}.{node.name}"
        symbol = SymbolRecord(
            id=self._make_id(f"class:{qualified_name}", node.lineno),
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
        self._class_attr_types.append(self._collect_class_attr_types(node))
        self._var_types.append({})
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self._var_types.pop()
            self._class_attr_types.pop()
            self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """记录函数或方法，并在其作用域内收集调用。"""
        parent = self.current_scope
        kind = "method" if parent.kind == "class" else "function"
        qualified_name = f"{parent.qualified_name}.{node.name}"
        symbol = SymbolRecord(
            id=self._make_id(f"{kind}:{qualified_name}", node.lineno),
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

        param_types: dict[str, str] = {}
        for arg in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ):
            t = _extract_type_name(arg.annotation)
            if t:
                param_types[arg.arg] = t
        if node.args.vararg is not None:
            t = _extract_type_name(node.args.vararg.annotation)
            if t:
                param_types[node.args.vararg.arg] = t
        if node.args.kwarg is not None:
            t = _extract_type_name(node.args.kwarg.annotation)
            if t:
                param_types[node.args.kwarg.arg] = t

        self._scope.append(symbol)
        self._var_types.append(param_types)
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self._var_types.pop()
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

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """记录带类型注解的局部变量或 self 属性类型。"""
        inferred = _extract_type_name(node.annotation) or _infer_value_type(
            node.value, self._var_types
        )
        if inferred:
            if isinstance(node.target, ast.Name):
                self._var_types[-1][node.target.id] = inferred
            elif (
                isinstance(node.target, ast.Attribute)
                and isinstance(node.target.value, ast.Name)
                and node.target.value.id == "self"
                and self._class_attr_types
            ):
                self._class_attr_types[-1][node.target.attr] = inferred
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """记录字面量或直接类构造的静态类型绑定（不推导普通函数返回值）。"""
        inferred = _infer_value_type(node.value, self._var_types)
        if inferred:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._var_types[-1][target.id] = inferred
                elif (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and self._class_attr_types
                ):
                    self._class_attr_types[-1].setdefault(target.attr, inferred)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """记录调用表达式，并利用已收集的静态类型注解/类属性改写接收者类型。"""
        raw_expr = _safe_unparse(node.func)
        callee_name = self._typed_call_name(node.func)
        self.calls.append(
            CallRecord(
                caller_id=self.current_scope.id,
                callee_expression=raw_expr,
                callee_name=callee_name,
                line=node.lineno,
                column=node.col_offset,
            )
        )
        self.generic_visit(node)

    def _typed_call_name(self, func: ast.AST) -> str:
        """将 ``self.store.save`` / ``manager.load`` / ``"\\n".join`` 映射为可静态解析的符号名。"""
        if isinstance(func, ast.Attribute):
            # 1. 内建字面量接收者："\n".join(...) / f"{x}".strip()
            if isinstance(func.value, ast.Constant):
                lit_type = _LITERAL_BUILTIN_TYPES.get(type(func.value.value))
                if lit_type:
                    return f"{lit_type}.{func.attr}"
            if isinstance(func.value, ast.JoinedStr):
                return f"str.{func.attr}"
            if isinstance(func.value, (ast.List, ast.ListComp)):
                return f"list.{func.attr}"
            if isinstance(func.value, (ast.Dict, ast.DictComp)):
                return f"dict.{func.attr}"
            if isinstance(func.value, (ast.Set, ast.SetComp)):
                return f"set.{func.attr}"

            # 2. self.<attr>.<method>(...) -> TypeName.<method>
            if (
                isinstance(func.value, ast.Attribute)
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "self"
            ):
                attr_type = self._lookup_self_attr_type(func.value.attr)
                if attr_type:
                    return f"{attr_type}.{func.attr}"

            # 3. <var>.<method>(...) -> TypeName.<method>
            if isinstance(func.value, ast.Name) and func.value.id not in (
                "self",
                "cls",
            ):
                var_type = self._lookup_var_type(func.value.id)
                if var_type:
                    return f"{var_type}.{func.attr}"

        return _call_name(func)


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
